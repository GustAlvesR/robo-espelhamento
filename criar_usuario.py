"""Gera a linha para colar na aba Usuarios_Site (Email | Nome | Senha_Hash | Ativo).
Uso: python criar_usuario.py email@empresa.com "Nome da Pessoa"
A senha é pedida sem aparecer na tela e nunca vai para a planilha em texto puro."""

import getpass
import sys

from werkzeug.security import generate_password_hash

if len(sys.argv) < 3:
    sys.exit('Uso: python criar_usuario.py email@empresa.com "Nome da Pessoa"')

email, nome = sys.argv[1].strip().lower(), sys.argv[2].strip()
senha = getpass.getpass("Senha (mín. 10 caracteres): ")
if len(senha) < 10:
    sys.exit("Senha curta demais.")
if senha != getpass.getpass("Repita a senha: "):
    sys.exit("As senhas não conferem.")

print("\nCole estas 4 células numa linha nova da aba Usuarios_Site:\n")
print("\t".join([email, nome, generate_password_hash(senha), "Sim"]))
