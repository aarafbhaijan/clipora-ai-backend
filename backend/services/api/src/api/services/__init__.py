"""Business rules behind the routes. Routes translate HTTP to these calls and back; nothing here knows about HTTP
except raising AppError, which the app turns into the error envelope."""
