from datetime import datetime
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field


class HealthCheckResponse(BaseModel):
    status: str = Field(default="healthy", description="Application health status")
    app_name: str = Field(description="Name of the application")
    environment: str = Field(description="Current running environment")
    version: str = Field(description="Application version")
    database: str = Field(description="Database connectivity status")
    timestamp: datetime = Field(description="Current server UTC timestamp")
    details: Optional[Dict[str, Any]] = Field(default=None, description="Additional subsystem diagnostics")

