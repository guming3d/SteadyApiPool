"""What each setup check means, in the words the web wizard uses (setup-wizard.tsx)."""

from __future__ import annotations

from typing import Any

STEP_OF_KIND = {
    "sign_in": "Sign in",
    "azure_token": "Sign in",
    "apim_target": "APIM service",
    "apim_access": "Permissions",
    "discovery": "Permissions",
    "app_apim": "Permissions",
    "app_discovery": "Permissions",
    "apim_identity": "APIM → Foundry",
    "foundry_access": "APIM → Foundry",
}
ICONS = {"ok": "✓", "action": "!", "blocked": "✗", "pending": "…", "unknown": "?", "skipped": "–"}
FIXES = {
    "sign_in": "Sign in with the browser",
    "select_apim": "Choose the APIM service",
    "enable_apim_identity": "Enable APIM's managed identity",
    "grant_foundry_access": "Grant APIM access",
    "consent": "Allow Azure access (in the browser)",
    "refresh_token": "Renew the session (in the browser)",
}


SIGN_IN_SOURCES = {
    "browser": "this app's browser sign-in",
    "device_code": "this app's device-code sign-in",
    "azure_cli": "the Azure CLI",
    "azd": "the Azure Developer CLI",
    "powershell": "Azure PowerShell",
}


def sign_in_lines(local: dict[str, Any] | None) -> list[str]:
    """How the local app is signed in, as the web wizard shows it under the sign-in check."""
    if not local:
        return []
    lines = []
    source = local.get("source")
    if source:
        line = f"Signed in through {SIGN_IN_SOURCES.get(source, source)}"
        if source in {"browser", "device_code"}:
            line += ", kept after a restart" if local.get("remembered") else ", until the app stops"
        lines.append(line)
    if not local.get("persistent"):
        lines.append(
            "This machine can't encrypt a stored sign-in (typical on Linux or WSL without a"
            " keyring), so a sign-in made in the app is asked for again after a restart."
            " az login avoids that."
        )
    return lines


def _name(value: str | None) -> str:
    return (value or "").rstrip("/").rpartition("/")[2]


def title(check: dict[str, Any], report: dict[str, Any], apim: str) -> tuple[str, str | None]:
    """(title, hint) for a check."""
    kind, status = check["kind"], check["status"]
    subject = check.get("subject") or ""
    ok = status == "ok"
    user = (report.get("user") or {}).get("name") or check.get("detail") or ""
    if kind == "sign_in":
        if ok:
            return (f"Signed in: {user}" if user else "Signed in"), None
        tenant = str(check.get("detail") or "").startswith("signed in to tenant")
        return "Sign in to Azure", (
            "You're signed in to a different directory than the APIM service."
            if tenant
            else "The app uses your Azure account to read and change APIM."
        )
    if kind == "azure_token":
        if ok:
            return "The app can act on Azure as you", None
        if check.get("fix") == "refresh_token":
            return "Your Azure session expired", None
        if check.get("fix") == "consent":
            return "Allow the app to act on Azure as you", "Needed to fix access for you."
        return "Delegated Azure access is off", "Ask an administrator to turn it on."
    if kind == "apim_target":
        if ok:
            return f"Managing {subject}", None
        if status == "pending":
            return f"{subject} is being created ({check.get('detail')})", (
                "v2 tiers take a few minutes, Developer 30-40. This view refreshes itself."
            )
        if status == "blocked":
            return f"APIM service {subject} not found", "Choose or create another one."
        if status == "unknown":
            return f"Can't read APIM service {subject}", None
        return "Choose the APIM service to manage", (
            "Press c to pick one from your subscriptions."
            if check.get("fix")
            else "It is set by environment variables or backend/.env."
        )
    if kind == "apim_access":
        if ok:
            granted = check.get("granted_by")
            if granted == "owners":
                return "You can change pools: you are in the owners list", None
            if granted == "azure_rbac":
                return "You can change pools: your Azure roles allow it", None
            return f"You can read and change {apim}", None
        if status == "blocked":
            return f"You can't access {apim}", "At least read access to the APIM service is needed."
        if status == "unknown":
            return f"Can't check your access to {apim}", None
        return f"You can read {apim} but not change it", (
            "Changing pools needs write access to APIM backends, named values and API policies."
        )
    if kind == "discovery":
        if ok:
            return f"You can see Foundry deployments in {subject}", None
        return (
            f"You can't see Foundry deployments in {subject}",
            "Reader on the subscription is enough.",
        )
    if kind == "app_apim":
        if ok:
            return f"The app can manage {apim}", None
        return f"The app's identity can't {'access' if status == 'blocked' else 'change'} {apim}", (
            "Run azd provision again to restore its role."
        )
    if kind == "app_discovery":
        if ok:
            return f"The app can see Foundry deployments in {subject}", None
        return f"The app can't see Foundry deployments in {subject}", "Run azd provision again."
    if kind == "apim_identity":
        if ok:
            return "APIM has a managed identity", None
        if status == "pending":
            return "APIM is turning its managed identity on", "Takes a minute or two."
        if status == "unknown":
            return "Can't read the APIM service", None
        return "Enable APIM's managed identity", (
            "Pool members call Foundry as APIM; without it calls return 500."
        )
    if kind == "foundry_access":
        name = _name(subject)
        if ok:
            return f"APIM can call {name}", None
        if status == "skipped":
            return f"{name}: enable APIM's managed identity first", None
        return f"APIM can't call {name} yet", (
            "APIM's identity needs Cognitive Services OpenAI User here, otherwise calls return 401."
        )
    return kind, None


def needs_attention(check: dict[str, Any]) -> bool:
    return check["status"] in {"action", "blocked"}
