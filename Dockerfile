# seafobj: Seafiles eigene Python-Bibliothek zum Lesen von Commits, Verzeichnissen,
# Dateien und Blöcken (Apache-2.0, https://github.com/haiwen/seafobj).
# Nicht auf PyPI, deshalb auf einen Commit festgelegt (Branch 13.0).
ARG SEAFOBJ_COMMIT=794428d5f455338f2f2f472c1f0ee2eebf623103

FROM debian:trixie-slim AS seafobj
ARG SEAFOBJ_COMMIT
ADD https://github.com/haiwen/seafobj/archive/${SEAFOBJ_COMMIT}.tar.gz /tmp/seafobj.tar.gz
RUN mkdir /src && tar -xzf /tmp/seafobj.tar.gz -C /src --strip-components=1

FROM debian:trixie-slim AS runtime
# Ändert sich mit jedem Release, damit die Debian-Updates nicht aus dem Build-Cache kommen
ARG VERSION=dev
# Abhängigkeiten aus Debian statt pip: pylibmc ist eine C-Erweiterung, die seafobj
# fest importiert; so wird nichts kompiliert und Sicherheitsupdates kommen über Debian.
# lxml braucht objwrapper für S3. Alibaba OSS (oss2) ist nicht enthalten.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends \
      python3 python3-pymysql python3-requests \
      python3-sqlalchemy python3-redis python3-pylibmc python3-boto3 python3-lxml \
      ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV SEAFILE_ARCHIVER_VERSION=${VERSION} \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=seafobj /src/seafobj /app/seafobj
COPY --from=seafobj /src/objwrapper /app/objwrapper
COPY --from=seafobj /src/LICENSE.txt /usr/share/doc/seafobj/LICENSE.txt
COPY LICENSE /usr/share/doc/seafile-archiver/LICENSE
COPY src/seafile_archiver /app/seafile_archiver
RUN printf '#!/bin/sh\nexec python3 -m seafile_archiver "$@"\n' > /usr/local/bin/seafile-archiver \
 && chmod +x /usr/local/bin/seafile-archiver \
 && python3 -c "import pylibmc, redis, sqlalchemy, boto3, pymysql, requests, objwrapper.s3, seafile_archiver" \
 && python3 -m compileall -q /app/seafobj /app/objwrapper >/dev/null

VOLUME ["/data"]
ENTRYPOINT ["seafile-archiver"]
CMD ["serve"]

# Unit-Tests mit denselben Paketen wie im Image: docker build --target test .
FROM runtime AS test
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-pytest \
 && rm -rf /var/lib/apt/lists/*
COPY tests /tests
RUN python3 -m pytest -q -p no:cacheprovider /tests

# Letzte Stufe = Standardziel, ohne Tests
FROM runtime
