from fastapi import FastAPI

app = FastAPI(
    title="AgentOps - Incident Diagnosis Platform (Version 0)",
    description="Rule-based deterministic incident diagnosis backend",
    version="0.1.0",
)

@app.get("/")
def root():
	return {"message": "AgentOps API is running"}

@app.get("/health")
def health_check():
	return {"status": "ok", "version": "0.1.0"}

