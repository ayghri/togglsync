#!/bin/bash
podman run --rm -v ${PWD}/sync/apis/:/dist:Z -v ${PWD}/data:/data:Z swaggerapi/swagger-codegen-cli generate \
    -i /data/openapi_toggl.json \
    -l python \
    -o /dist/

# podman run --rm -v ${PWD}/sync/apis/:/dist:Z -v ${PWD}/data:/data:Z /bin/bash waggerapi/swagger-codegen-cli /bin/bash
