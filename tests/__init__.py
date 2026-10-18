import os
import tempfile

# Os testes gravam numa pasta temporária, nunca no storage do usuário.
os.environ["OLLAMAPILOT_STORAGE"] = tempfile.mkdtemp(prefix="ollamapilot-testes-")
