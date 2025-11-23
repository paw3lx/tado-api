"""FastAPI server for Tado temperature and humidity data"""
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from PyTado.interface import Tado
from typing import Dict, Any, Optional
import logging
import os
import threading

app = FastAPI(title="Tado API", version="1.0.0")
logger = logging.getLogger("uvicorn.error")

# Mount static files for the web UI
app.mount("/static", StaticFiles(directory="static"), name="static")

TOKEN_FILE_PATH = os.getenv("TADO_TOKEN_PATH", "./data/refresh_token")


class ActivationResponse(BaseModel):
    status: str
    message: str
    url: Optional[str] = None
    zone_count: Optional[int] = None


@app.on_event("startup")
async def startup_event():
    app.state.tado_lock = threading.Lock()

def get_tado_client() -> Tado:
    """Initialize Tado client with token file"""
    client = getattr(app.state, "tado_client", None)

    if client is None:
        try:
            client = Tado(token_file_path=TOKEN_FILE_PATH)
            app.state.tado_client = client
            return client
        except Exception as e:
            raise HTTPException(
                status_code=503,
                detail=f"Failed to initialize Tado client: {str(e)}"
            )
    return app.state.tado_client

@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the activation web UI"""
    with open("static/index.html", "r") as f:
        return f.read()


@app.get("/api")
async def api_root():
    """API information endpoint"""
    return {
        "message": "Tado API",
        "endpoints": {
            "/": "Web UI for device activation",
            "/activation/status": "Get activation status",
            "/activation/start": "Start device activation (get URL)",
            "/activation/complete": "Complete activation after user authenticates",
            "/zones": "Get all zones data",
            "/zones/{zone_id}": "Get specific zone data",
            "/health": "Health check"
        }
    }


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    token_exists = os.path.exists(TOKEN_FILE_PATH)

    # Check activation status if possible
    activation_status = "unknown"
    if token_exists:
        try:
            tado = Tado(token_file_path=TOKEN_FILE_PATH)
            activation_status = tado.device_activation_status()
        except:
            activation_status = "error"

    return {
        "status": "healthy" if token_exists and activation_status == "COMPLETED" else "unhealthy",
        "token_file_exists": token_exists,
        "activation_status": activation_status,
        "token_file_path": TOKEN_FILE_PATH
    }


@app.get("/activation/status", response_model=ActivationResponse)
async def get_activation_status():
    """Get current activation status"""
    try:
        tado = get_tado_client()
        status = tado.device_activation_status()

        logger.info(f"Activation status: {status}")
        logger.info(f"Token file path: {TOKEN_FILE_PATH}")
        logger.info(f"Tado client: {tado}")

        if status == "COMPLETED":
            # Verify token works
            try:
                zones = tado.get_zone_states()
                zone_count = len(zones.get("zoneStates", {}))
                return ActivationResponse(
                    status="completed",
                    message="Device is activated and ready",
                    zone_count=zone_count
                )
            except Exception as e:
                return ActivationResponse(
                    status="completed",
                    message=f"Device is activated but cannot fetch zones. {e}"
                )
        elif status == "PENDING":
            return ActivationResponse(
                status="pending",
                message="Activation in progress. Call /activation/start to get URL"
            )
        else:
            return ActivationResponse(
                status=status.lower(),
                message=f"Activation status: {status}"
            )
    except Exception as e:
        return ActivationResponse(
            status="not_started",
            message="Activation not started. Call /activation/start to begin"
        )


@app.post("/activation/start", response_model=ActivationResponse)
async def start_activation():
    """Start device activation and get authentication URL"""
    try:
        # Ensure data directory exists
        os.makedirs(os.path.dirname(TOKEN_FILE_PATH), exist_ok=True)
        #os.remove(TOKEN_FILE_PATH)

        # Create new Tado instance for this activation flow
        tado = get_tado_client()
        status = tado.device_activation_status()

        if status == "COMPLETED":
            zones = tado.get_zone_states()
            zone_count = len(zones.get("zoneStates", {}))
            return ActivationResponse(
                status="completed",
                message="Device is already activated",
                zone_count=zone_count
            )

        # Get activation URL
        url = tado.device_verification_url()

        return ActivationResponse(
            status="pending",
            message="Please open the URL in your browser and authenticate",
            url=url
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to start activation: {str(e)}"
        )


@app.post("/activation/complete", response_model=ActivationResponse)
async def complete_activation():
    """Complete activation after user has authenticated in browser"""
    import time

    try:
        tado = get_tado_client()

        # First check if already completed
        status = tado.device_activation_status()
        if status == "COMPLETED":
            zones = tado.get_zone_states()
            zone_count = len(zones.get("zoneStates", {}))
            return ActivationResponse(
                status="completed",
                message="Activation successful! Token saved and verified",
                zone_count=zone_count
            )

        # Trigger completion and poll with timeout
        max_attempts = 30  # 30 seconds total
        for attempt in range(max_attempts):
            try:
                # This call polls Tado's servers
                tado.device_activation()

                # Check if completed
                status = tado.device_activation_status()

                if status == "COMPLETED":
                    # Verify token works
                    zones = tado.get_zone_states()
                    zone_count = len(zones.get("zoneStates", {}))

                    return ActivationResponse(
                        status="completed",
                        message="Activation successful! Token saved and verified",
                        zone_count=zone_count
                    )
                elif status == "PENDING":
                    # Still waiting, try again
                    time.sleep(1)
                    continue
                else:
                    # Some other status
                    break

            except Exception as inner_e:
                # If polling fails, wait and retry
                if attempt < max_attempts - 1:
                    time.sleep(1)
                    continue
                else:
                    raise inner_e

        # If we get here, activation didn't complete in time
        return ActivationResponse(
            status="pending",
            message=f"Activation not complete yet. Status: {status}. Please make sure you completed authentication in the browser and try again."
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to complete activation: {str(e)}"
        )
    
@app.post("/activation/reset", response_model=ActivationResponse)
async def reset_activation():
    """Reset activation process"""
    try:
        # Ensure data directory exists
        os.makedirs(os.path.dirname(TOKEN_FILE_PATH), exist_ok=True)
        os.remove(TOKEN_FILE_PATH)

        client = Tado(token_file_path=TOKEN_FILE_PATH)
        app.state.tado_client = client

        return ActivationResponse(
            status="reset",
            message="Activation process has been reset. You can start over."
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to reset activation: {str(e)}"
        )


@app.get("/zones")
async def get_all_zones() -> Dict[str, Any]:
    """Get temperature and humidity data for all zones"""
    tado = get_tado_client()

    try:
        zones = tado.get_zone_states()
        result = {}

        for zone_id, zone_data in zones["zoneStates"].items():
            sensor_data = zone_data.get("sensorDataPoints", {})

            # Get temperature data
            temp_data = sensor_data.get("insideTemperature", {})
            temperature_celsius = temp_data.get("celsius")
            temp_timestamp = temp_data.get("timestamp")

            # Get humidity data
            humidity_data = sensor_data.get("humidity", {})
            humidity_percentage = humidity_data.get("percentage")
            humidity_timestamp = humidity_data.get("timestamp")

            result[zone_id] = {
                "temperature": {
                    "celsius": temperature_celsius,
                    "timestamp": temp_timestamp
                },
                "humidity": {
                    "percentage": humidity_percentage,
                    "timestamp": humidity_timestamp
                }
            }

        return {"zones": result}

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve zone data: {str(e)}"
        )


@app.get("/zones/{zone_id}")
async def get_zone(zone_id: str) -> Dict[str, Any]:
    """Get temperature and humidity data for a specific zone"""
    tado = get_tado_client()

    try:
        zones = tado.get_zone_states()

        if zone_id not in zones["zoneStates"]:
            raise HTTPException(
                status_code=404,
                detail=f"Zone {zone_id} not found"
            )

        zone_data = zones["zoneStates"][zone_id]
        sensor_data = zone_data.get("sensorDataPoints", {})

        # Get temperature data
        temp_data = sensor_data.get("insideTemperature", {})
        temperature_celsius = temp_data.get("celsius")
        temp_timestamp = temp_data.get("timestamp")

        # Get humidity data
        humidity_data = sensor_data.get("humidity", {})
        humidity_percentage = humidity_data.get("percentage")
        humidity_timestamp = humidity_data.get("timestamp")

        return {
            "zone_id": zone_id,
            "temperature": {
                "celsius": temperature_celsius,
                "timestamp": temp_timestamp
            },
            "humidity": {
                "percentage": humidity_percentage,
                "timestamp": humidity_timestamp
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve zone data: {str(e)}"
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
