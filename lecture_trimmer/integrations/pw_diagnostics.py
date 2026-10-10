"""Small user-triggered PW checks; no lecture data, keys, or account details saved."""
import json
import logging
import time
from datetime import datetime, timezone

import lecture_trimmer.config as config
from lecture_trimmer.models import LessonMap
from lecture_trimmer.integrations.pw_proxy import ProxyCallError, parse_gemini_response, response_structure

logger = logging.getLogger("trimmer.pw")


def check_connection_events(job):
    job.filename = "connection-check"
    report = {"task_id": job.task_id, "model": config.GEMINI_MODEL,
              "checked_at": datetime.now(timezone.utc).isoformat(), "checks": []}
    def save():
        directory = config.TEMP_DIR / "pw_diagnostics"
        directory.mkdir(exist_ok=True)
        path = directory / (job.task_id + ".json")
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary = [f"PW access: **approved**. Gemini model: **{config.GEMINI_MODEL}**."]
    for name, body in [
        ("Basic Gemini request", {"contents": [{"role": "user", "parts": [{"text": "Reply with OK."}]}]}),
        ("Lecture classification format", {
            "contents": [{"role": "user", "parts": [{"text":
                "Map this one teaching activity: [unit_id=0] Velocity is displacement divided by time. "
                "Return one theory activity with first_id=0 and last_id=0, no dependencies, and no visual review."}]}],
            "systemInstruction": {"parts": [{"text": "Classify the supplied synthetic teaching activity."}]},
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                                 "responseJsonSchema": LessonMap.model_json_schema()},
        }),
    ]:
        yield "\n\n".join([*summary, f"**{name}: checking…**"])
        started = time.monotonic()
        response = None
        try:
            if name == "Basic Gemini request":
                response = job.session.generate(model=config.GEMINI_MODEL, request=body, task_id=job.task_id,
                                                filename=job.filename, count=1)
                parsed = parse_gemini_response(response)
            else:
                from google.genai import types
                client = job.client("gemini")
                try:
                    parsed = client.models.generate_content(model=config.GEMINI_MODEL,
                        contents=body["contents"][0]["parts"][0]["text"],
                        config=types.GenerateContentConfig(
                            system_instruction="Classify the supplied activity. Return JSON matching the supplied schema.",
                            response_mime_type="application/json", response_schema=LessonMap, temperature=0,
                            http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
                            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
                finally:
                    client.close()
            if name == "Lecture classification format":
                mapped = LessonMap.model_validate_json(parsed.text)
                if len(mapped.activities) != 1 or mapped.activities[0].first_id != 0 or mapped.activities[0].last_id != 0:
                    raise ValueError("Invalid test classification")
            report["checks"].append({"check": name, "status": "passed", "seconds": round(time.monotonic()-started, 2)})
            summary.append(f"{name}: **passed**.")
        except ProxyCallError as exc:
            report["checks"].append({"check": name, "status": "failed", **exc.diagnostic()})
            summary.append(f"{name}: **failed**. {exc.public_message}")
            save()
            yield "\n\n".join([*summary, f"Diagnostic reference: `{job.task_id}`. The report is saved locally."])
            return
        except Exception as exc:
            detail = {"check": name, "status": "failed", "category": "invalid_response",
                      "exception_type": type(exc).__name__, "response_structure": response_structure(response)}
            from pydantic import ValidationError
            if isinstance(exc, ValidationError):
                detail["validation_errors"] = [{"type": item["type"], "location": list(item["loc"])}
                    for item in exc.errors(include_input=False, include_context=False)[:10]]
            report["checks"].append(detail)
            save()
            yield "\n\n".join([*summary, f"{name}: **failed**. PW returned an unexpected response format.",
                                f"Diagnostic reference: `{job.task_id}`."])
            return
        save()
    logger.info("PW connection checks passed: task_id=%s model=%s", job.task_id, config.GEMINI_MODEL)
    yield "\n\n".join([*summary, "The small connection checks passed. You can retry the lecture with Start fresh left off."])
