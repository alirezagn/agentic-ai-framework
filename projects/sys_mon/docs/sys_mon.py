import json
import psutil
import sys

def get_system_metrics():
    """Fetches CPU, RAM, and Disk usage metrics."""
    try:
        # CPU Usage
        cpu_usage = psutil.cpu_percent(interval=1)
        
        # RAM Usage
        virtual_mem = psutil.virtual_memory()
        ram_usage = {
            "total_gb": round(virtual_mem.total / (1024**3), 2),
            "available_gb": round(virtual_mem.available / (1024**3), 2),
            "percent_used": virtual_mem.percent
        }
        
        # Disk Usage (Root partition)
        disk = psutil.disk_usage('/')
        disk_usage = {
            "total_gb": round(disk.total / (1024**3), 2),
            "used_gb": round(disk.used / (1024**3), 2),
            "free_gb": round(disk.free / (1024**3), 2),
            "percent_used": disk.percent
        }
        
        return {
            "cpu_percent": cpu_usage,
            "ram": ram_usage,
            "disk": disk_usage
        }
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    metrics = get_system_metrics()
    print(json.dumps(metrics, indent=2))
    sys.exit(0)