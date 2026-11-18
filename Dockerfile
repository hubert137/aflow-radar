FROM public.ecr.aws/docker/library/python@sha256:31afb5c02f25b87e006154aaa615a394ab6b955b0f9d48c02612e6c7357b3b52
ENV TZ=Asia/Shanghai PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock && mkdir -p /app/static && python -c "import plotly,pathlib,shutil; shutil.copy(pathlib.Path(plotly.__file__).parent/'package_data/plotly.min.js','/app/static/plotly.min.js')"
COPY src/fund_flow.py /app/fund_flow.py
USER 10001:10001
CMD ["python","fund_flow.py","--live","--interval","300","--top","30","--bottom","15","--mode","cumulative","--port","8899"]
