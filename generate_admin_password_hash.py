from getpass import getpass

from werkzeug.security import generate_password_hash


password = getpass("Nowe hasło administratora: ")
confirmation = getpass("Powtórz hasło: ")
if password != confirmation:
    raise SystemExit("Hasła są różne.")
if len(password) < 12:
    raise SystemExit("Hasło musi mieć co najmniej 12 znaków.")

print("\nWklej poniższą wartość do ADMIN_PASSWORD_HASH w Render:\n")
print(generate_password_hash(password))

