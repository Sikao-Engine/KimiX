import asyncio
import subprocess
import sys
from kimix.tools.common import _maybe_export_output
from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field
from pathlib import Path


class Params(BaseModel):
    file_path: str = Field(
        description="Python file path to check."
    )


class PySyntaxCheck(CallableTool2):
    name: str = "PySyntaxCheck"
    description: str = "Check Python syntax with ruff."
    params: type[Params] = Params

    async def __call__(self, params: Params) -> ToolReturnValue:
        # Try to import ruff, install if not available
        try:
            import ruff
        except ImportError:
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", "ruff"])
                import ruff
            except Exception as e:
                return ToolError(
                    message=f"Failed to install ruff: {str(e)}",
                    brief="Ruff installation failed"
                )

        # Read code from file and use it for ruff analysis
        try:
            file_path = Path(params.file_path)
            if not file_path.exists():
                return ToolError(
                    message=f"File not found: {params.file_path}",
                    brief="File not found"
                )
            
            # Run ruff check to get errors and warnings
            result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", "ruff", "check", str(file_path), "--output-format=json"],
                capture_output=True,
                text=True
            )

            import orjson
            errors = []
            warnings = []
            hints = []

            if result.stdout:
                try:
                    diagnostics = orjson.loads(result.stdout)
                    for diag in diagnostics:
                        message = diag.get('message', '')
                        code = diag.get('code', '')
                        severity = diag.get('severity', 'error')
                        location = f"Line {diag.get('location', {}).get('row', '?')}, Col {diag.get('location', {}).get('column', '?')}"
                        
                        item = f"[{code}] {message} ({location})"
                        
                        if severity == 'error':
                            errors.append(item)
                        elif severity == 'warning':
                            warnings.append(item)
                        else:
                            hints.append(item)
                except orjson.JSONDecodeError:
                    pass

            # Also check for formatting issues as hints
            fmt_result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", "ruff", "format", str(file_path), "--check", "--output-format=json"],
                capture_output=True,
                text=True
            )

            if fmt_result.stdout:
                try:
                    fmt_diagnostics = orjson.loads(fmt_result.stdout)
                    for diag in fmt_diagnostics:
                        message = diag.get('message', 'Formatting issue')
                        location = f"Line {diag.get('start_location', {}).get('row', '?')}"
                        hints.append(f"[format] {message} ({location})")
                except orjson.JSONDecodeError:
                    pass

            output_parts = []
            if errors:
                output_parts.append("Errors:\n" + "\n".join(f"  - {e}" for e in errors))
            if warnings:
                output_parts.append("Warnings:\n" + "\n".join(f"  - {w}" for w in warnings))
            if hints:
                output_parts.append("Hints:\n" + "\n".join(f"  - {h}" for h in hints))

            if not output_parts:
                output = "No issues found. Code looks good!"
            else:
                output = "\n\n".join(output_parts)

            output = _maybe_export_output(output)
            return ToolOk(output=output)

        except Exception as e:
            return ToolError(message=str(e), brief="PySyntaxCheck error")
