# Disposable local controller/guardian image. Hub itself never receives this socket.
ARG HUB_IMAGE=waypoint-authority-hub:local
FROM ${HUB_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends openssl && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/authority
COPY ownership_contract.py authority_access.py authority_operator.py runtime_authority.py runtime_guardian.py controller_service.py /srv/authority/
COPY worker_enrollment.py worker_credentials.py /srv/authority/
COPY hub/authority_client.py /srv/authority/hub/authority_client.py
COPY tests/authority_test_pki.py /srv/authority/authority_test_pki.py
COPY rehearsal/authenticated_fixture.py rehearsal/enrollment_fixture.py /srv/authority/
CMD ["python", "controller_service.py"]
