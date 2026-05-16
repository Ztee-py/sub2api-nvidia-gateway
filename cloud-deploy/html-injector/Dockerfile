FROM public.ecr.aws/docker/library/python:3.13-alpine

WORKDIR /app
COPY server.py /app/server.py

ENV BIND_HOST=0.0.0.0
ENV PORT=8090
ENV UPSTREAM_URL=http://sub2api:8080

EXPOSE 8090
CMD ["python", "/app/server.py"]
