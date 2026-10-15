# Disposable local controller/guardian image. Hub itself never receives this socket.
ARG HUB_IMAGE=waypoint-authority-hub:local
FROM ${HUB_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends openssl && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/authority
COPY ownership_contract.py authority_access.py authority_operator.py runtime_authority.py runtime_guardian.py controller_service.py /srv/authority/
COPY worker_enrollment.py worker_credentials.py /srv/authority/
COPY remote_backend.py remote_control.py remote_runtime_service.py remote_credentials.py worker_transport.py worker_runtime.py worker_channel_state.py volume_archive.py /srv/authority/
COPY hub/authority_client.py /srv/authority/hub/authority_client.py
COPY tests/authority_test_pki.py /srv/authority/authority_test_pki.py
COPY rehearsal/authenticated_fixture.py rehearsal/enrollment_fixture.py /srv/authority/
COPY rehearsal/authenticated_evidence.py rehearsal/remote_notebook_fixture.py /srv/authority/
CMD ["python", "controller_service.py"]
