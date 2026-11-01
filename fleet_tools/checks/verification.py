from fleet_tools import verification
from fleet_tools.checks.context import Advisory, Check, Context
from fleet_tools.config import ConfigError

KEY = "verification"
DEFAULT_EXEMPT = ["dependabot[bot]", "fleet-bot[bot]"]
WHERE = "the pull request's description"


def escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def settings(context: Context) -> tuple[bool, list[str]]:
    table = context.config.section(KEY)
    required = table.get("required", False)
    if not isinstance(required, bool):
        raise ConfigError(f"[tool.fleet.{KEY}] required must be true or false")
    exempt = table.get("exempt-authors", DEFAULT_EXEMPT)
    if not isinstance(exempt, list) or not all(isinstance(author, str) for author in exempt):
        raise ConfigError(f"[tool.fleet.{KEY}] exempt-authors must be a list of strings")
    return required, list(exempt)


def check_description(body: str, required: bool, draft: bool) -> list[str]:
    try:
        verification.parse(body)
    except verification.ParseError as error:
        message = escape(f"{WHERE}, line {error.line}: {error.rule} (the fleet-verify-change skill writes the "
                         "Verification section)")
        return [message] if required and not draft else [Advisory(message)]
    return []


def applies(context: Context) -> bool:
    return context.config.has(KEY) and context.pull_request is not None


def run(context: Context) -> list[str]:
    pull_request = context.pull_request
    if pull_request is None:
        return []
    required, exempt = settings(context)
    if pull_request.author in exempt:
        return []
    return check_description(pull_request.body, required, pull_request.draft)


CHECK = Check("verification", "a verification record in the description", applies, run)
