import warnings


def suppress_known_runtime_warnings() -> None:
    warnings.filterwarnings(
        "ignore",
        message=r"You are using a Python version \(3\.10\..*\) which Google will stop supporting.*",
        category=FutureWarning,
        module=r"google\.api_core\._python_version_support",
    )
