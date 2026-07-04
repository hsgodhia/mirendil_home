ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim AS nltk-eval-base

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Pin the exact commit this environment represents: the state of nltk/develop
# immediately before PR #3564 ("Align TnT with Brants (2000) specifications")
# was merged (first parent of merge commit 6469c307dfd115ad1b975f07b46b5c45fc236acb).
ARG NLTK_COMMIT=a51b230b785ba37f0995152c74875f19f10302d0
ENV NLTK_COMMIT=${NLTK_COMMIT}

RUN git clone https://github.com/nltk/nltk.git /repo \
 && cd /repo \
 && git checkout ${NLTK_COMMIT}

WORKDIR /repo
RUN pip install --no-cache-dir -e ".[all]" pytest pytest-cov pytest-mock pytest-json-report

# Bake nltk_data into the image at build time so eval containers can run
# with --network=none.
ENV NLTK_DATA=/usr/share/nltk_data
RUN python -m nltk.downloader -d /usr/share/nltk_data all

COPY run_eval.py /opt/run_eval.py
COPY entrypoint.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh

ENTRYPOINT ["/opt/entrypoint.sh"]
