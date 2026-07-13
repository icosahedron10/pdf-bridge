"""Thin pull/report orchestration for the Jenkins job client."""

from __future__ import annotations

import uuid
from pathlib import Path

from pdf_bridge.contracts.job_contracts import ClientOptions, PullResult
from pdf_bridge.contracts.schemas import BatchClaimRequest, BatchResultsResponse, BatchStageRequest
from pdf_bridge.services import job_http, job_staging


def pull_batch(
    *,
    destination: Path,
    request_id: str | None,
    limit: int,
    result_file: Path | None,
    client_options: ClientOptions,
) -> PullResult:
    """Claim, validate, download, and acknowledge one Jenkins batch."""

    stable_request_id = request_id or f"manual-{uuid.uuid4()}"
    claim_request = BatchClaimRequest(request_id=stable_request_id, limit=limit)
    destination_root = job_staging._validate_destination_root(destination)

    with job_http.client_from_options(client_options) as client:
        claim = client.claim(claim_request)
        if claim is None:
            result = PullResult(
                batch_id=None,
                request_id=stable_request_id,
                operation_count=0,
                batch_directory=None,
            )
        else:
            # Correlate the remote claim before writing anything locally, then
            # acknowledge only after the complete batch is durably promoted.
            remote = client.manifest(claim.batch_id)
            job_staging.validate_claim_manifest(
                claim,
                remote,
                request_id=stable_request_id,
            )
            if not remote.operations:
                result = PullResult(
                    batch_id=remote.batch_id,
                    request_id=stable_request_id,
                    operation_count=0,
                    batch_directory=None,
                    idempotent_replay=claim.idempotent_replay,
                )
            else:
                local = job_staging._local_manifest(remote)
                final_directory, manifest_sha256 = job_staging._stage_new_batch(
                    client,
                    destination_root,
                    remote,
                    local,
                )
                operation_ids = [item.operation_id for item in remote.operations]
                stage_response = client.acknowledge_staged(
                    remote.batch_id,
                    BatchStageRequest(operation_ids=operation_ids),
                )
                result = PullResult(
                    batch_id=remote.batch_id,
                    request_id=stable_request_id,
                    operation_count=len(remote.operations),
                    batch_directory=str(final_directory),
                    manifest_sha256=manifest_sha256,
                    idempotent_replay=(
                        claim.idempotent_replay or stage_response.idempotent_replay
                    ),
                )

    if result_file is not None:
        job_staging._write_json_result(result_file, result)
    return result


def report_batch(
    *,
    report_path: Path,
    pull_result_path: Path,
    client_options: ClientOptions,
) -> BatchResultsResponse:
    """Validate local result files, submit them, and verify the response batch."""

    parsed, request = job_staging.prepare_report_submission(
        report_path,
        pull_result_path,
    )
    with job_http.client_from_options(client_options) as client:
        response = client.report_results(parsed.batch_id, request)
    job_staging.validate_report_response(
        response,
        expected_batch_id=parsed.batch_id,
    )
    return response
