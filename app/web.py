from fastapi import FastAPI
from fastapi.responses import JSONResponse

def make_app():
    app=FastAPI(title="JARVIS Index Bot")
    @app.get("/")
    async def root(): return {"service":"JARVIS Index Bot","status":"running"}
    @app.get("/health")
    async def health(): return JSONResponse({"status":"ok"})
    return app
