from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from pdf_bridge.persistence.db import Base, build_engine


@pytest.fixture
def session() -> Session:
    engine = build_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as active_session:
        yield active_session
    engine.dispose()
