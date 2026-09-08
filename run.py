"""
QBO AI Controller Agent — Entry Point
Run: python run.py
"""
import uvicorn
from app.config import settings

if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║         QBO AI CONTROLLER AGENT — STARTING UP               ║
╠══════════════════════════════════════════════════════════════╣
║  App:         {settings.APP_NAME:<45} ║
║  Host:        {settings.APP_HOST:<45} ║
║  Port:        {str(settings.APP_PORT):<45} ║
║  Environment: {settings.QBO_ENVIRONMENT:<45} ║
║  URL:         http://{settings.APP_HOST}:{settings.APP_PORT:<38} ║
╚══════════════════════════════════════════════════════════════╝
""")
    uvicorn.run(
        "app.main:app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        reload=settings.DEBUG,
        log_level="info"
    )
