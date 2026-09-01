"""Create two test user accounts."""
import sys; sys.path.insert(0, ".")
from app.database import SessionLocal
from app.models import User
from app.services.auth_service import auth_service

db = SessionLocal()

users_to_create = [
    {"email": "test1@zhanlu.dev", "full_name": "Test User 1", "password": "test1234"},
    {"email": "test2@zhanlu.dev", "full_name": "Test User 2", "password": "test1234"},
]

for u in users_to_create:
    existing = db.query(User).filter(User.email == u["email"]).first()
    if existing:
        print(f"SKIP: {u['email']} already exists")
        continue
    user = User(
        email=u["email"],
        full_name=u["full_name"],
        role="user",
        password_hash=auth_service.hash_password(u["password"]),
    )
    db.add(user)
    db.flush()
    print(f"CREATED: {u['email']} / {u['password']} (id={user.id})")

db.commit()
db.close()
print("DONE")
