#!/usr/bin/env python3
"""
AI Persona System - launch script
"""

import uvicorn
import sys
from pathlib import Path
from dotenv import load_dotenv

# Add the project root to sys.path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# Load environment variables from .env
load_dotenv()

if __name__ == "__main__":
    uvicorn.run(
        "web.main:app",
        # Local login (AUTH_MODE=local) has no verification, so accept connections from this machine only.
        host="127.0.0.1",
        port=8000,
        reload=True,
        reload_dirs=["web", "src"],
        # Same settings as the Dockerfile CMD. uvicorn access logs include the query (code, state),
        # so disable them and use the app's http.access log instead.
        access_log=False,
        timeout_keep_alive=310,
    )
