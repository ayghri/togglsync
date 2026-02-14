podman run --rm -v ${PWD}/sync/apis/server:/dist:Z -v ${PWD}/data:/data:Z swaggerapi/swagger-codegen-server generate \
    -i /data/openapi_toggl.json \
    -l python \
    -o /dist/


