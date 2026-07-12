[project]
name = "quantum-sim"
version = "0.1.0"
description = "Quantum computer simulator with realistic software stack"
requires-python = ">=3.11"

[tool.ruff]
line-length = 88
target-version = "py311"
select = ["E", "F", "I", "UP", "B", "SIM"]
ignore = ["E501"]

[tool.mypy]
python_version = "3.11"
strict = true
ignore_missing_imports = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "--cov=quantum_sim --cov-report=term-missing"

[tool.coverage.run]
source = ["quantum_sim"]
omit = ["*/tests/*"]
