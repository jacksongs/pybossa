FROM python:2.7-alpine

ENV REDIS_SENTINEL=redis-sentinel
ENV REDIS_MASTER=mymaster

# install git and various python library dependencies with alpine tools 
RUN set -x && \
    apk --no-cache add postgresql-dev g++ gcc git jpeg-dev libffi-dev libjpeg libxml2-dev libxslt-dev linux-headers musl-dev openssl zlib zlib-dev

# This is for LDAP support in pybossa 2.8. The two steps can be combined with openldap-dev replacing openldap at the next opportunity.
RUN set -x && \
	apk --no-cache add openldap openldap-clients openldap-back-hdb openldap-back-bdb ldapvi

RUN set -x && \
	apk --no-cache add openldap-dev

# Create the working directory
RUN mkdir -p /usr/src/app/pybossa
ADD requirements.txt /usr/src/app/pybossa
ADD setup.py /usr/src/app/pybossa

# Install the package dependencies (this step is separated
# from copying all the source code to avoid having to
# re-install all python packages defined in requirements.txt
# whenever any source code change is made)
ENV LIBRARY_PATH=/lib:/usr/lib
RUN set -x && \
    cd /usr/src/app/pybossa && \
    pip install -U pip setuptools && \
    pip install -r /usr/src/app/pybossa/requirements.txt && \
    rm -rf /usr/src/app/pybossa/.git/ && \
    addgroup pybossa  && \
    adduser -D -G pybossa -s /bin/sh -h /usr/src/app/pybossa pybossa && \
    passwd -u pybossa

# I got this error: ImportError: C extension: umpy.core.multiarray failed to import not built.
# it required a numpy/pandas uninstall/install
RUN set -x && \
    pip uninstall -y numpy && \
    pip uninstall -y pandas && \
    pip install numpy && \
    pip install pandas

RUN echo a!!

COPY . /usr/src/app/pybossa

# TODO: we shouldn't need write permissions on the whole folder
#   Known files written during runtime:
#     - /opt/pybossa/pybossa/themes/default/static/.webassets-cache
#     - /opt/pybossa/alembic.ini and /opt/pybossa/settings_local.py (from entrypoint.sh)
RUN chown -R pybossa:pybossa /usr/src/app/pybossa

ADD entrypoint-local.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

# run with unprivileged user
USER pybossa
WORKDIR /usr/src/app/pybossa
EXPOSE 5000

# Background worker is also necessary and should be run from another copy of this container
#   python app_context_rqworker.py scheduled_jobs super high medium low email maintenance
# CMD ["python", "run.py"] 