# ---- base ----
FROM python:3.12-slim AS base
WORKDIR /app
RUN pip install pipenv

# ---- dev ----
FROM base AS dev
COPY Pipfile Pipfile.lock ./
RUN pipenv install --dev --system --deploy
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---- prod ----
FROM base AS prod
COPY Pipfile Pipfile.lock ./
RUN pipenv install --system --deploy
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
