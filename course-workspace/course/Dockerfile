FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
# Python and R are part of the course image, never installed in ephemeral startup.
RUN apt-get update && apt-get install -y --no-install-recommends r-base r-cran-irkernel r-cran-ggplot2 r-cran-dplyr r-cran-readr git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt && R -e 'IRkernel::installspec(user=FALSE)'
RUN useradd --create-home --uid 1000 --shell /bin/bash learner && mkdir -p /home/learner/work && chown -R 1000:1000 /home/learner
COPY --chown=1000:1000 Welcome.ipynb /home/learner/work/Welcome.ipynb
ENV HOME=/home/learner MPLCONFIGDIR=/home/learner/.cache/matplotlib
USER 1000:1000
WORKDIR /home/learner/work
CMD ["jupyterhub-singleuser"]
