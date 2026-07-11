FROM clamav/clamav:1.5.3

# The clamd INSTREAM default is smaller than PDF Bridge's 50 MiB upload limit.
# Keep one explicit ceiling so a file accepted by the app can always be scanned.
RUN if grep -Eq '^[#[:space:]]*StreamMaxLength' /etc/clamav/clamd.conf; then \
      sed -Ei 's/^[#[:space:]]*StreamMaxLength.*/StreamMaxLength 64M/' /etc/clamav/clamd.conf; \
    else \
      printf '\nStreamMaxLength 64M\n' >> /etc/clamav/clamd.conf; \
    fi
