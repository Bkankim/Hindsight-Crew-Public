# Gateway image — pin base by digest in compose via build arg in Stage 2.
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app/gateway
ENV HC_ACL_FILE=/secrets/acl.json HC_AUDIT_LOG=/audit/gateway.log
EXPOSE 8888
# server.py (Stage 2) wires create_app() from env; placeholder entry until then.
CMD ["python","-c","print('gateway image ok; server entrypoint wired in Stage 2')"]
