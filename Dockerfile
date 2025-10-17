FROM python:3.13-slim

RUN apt update && apt install -y nodejs && apt clean
ADD requirements.txt /opt/
RUN pip install --no-cache -r /opt/requirements.txt
ADD xzspider2.py acw_sc_v2.js adapter.js /opt/

WORKDIR /opt/
ENTRYPOINT [ "/opt/xzspider2.py", "-o", "/opt/xzdocs"]
