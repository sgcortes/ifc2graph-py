"""
Lanzador de la web app IFC2GRAPH.
Uso: python run.py
Luego abre http://localhost:8000 en el navegador.
"""
import subprocess, sys, os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

print("=" * 50)
print("  IFC2GRAPH Web App")
print("  http://localhost:8000")
print("=" * 50)

subprocess.run([
    sys.executable, "-m", "uvicorn", "app:app",
    "--host", "127.0.0.1", "--port", "8000", "--reload",
])
