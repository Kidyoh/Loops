from fastapi import FastAPI

app = FastAPI(title="Loops")


@app.get("/health")
async def health() -> dict:
    return {"ok": True}
