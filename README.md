# Scienthesis Backend

FastAPI backend for the Scienthesis literature digest platform.

## Features

### Stage 1 (Complete)
- Health check endpoints
- MongoDB connection with Motor
- Flexible port configuration
- CORS middleware

### Stage 2 (Complete)
- JWT-based authentication
- User management (signup, login, verification)
- Email flows (SendGrid integration)
- Password reset functionality
- Secure password hashing (bcrypt)

## Required Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `MONGO_URL` | MongoDB connection string | `mongodb://localhost:27017` |
| `DB_NAME` | MongoDB database name | `scienthesis_db` |
| `CORS_ORIGINS` | Comma-separated allowed origins | `*` |
| `PORT` | Port to bind the server | `8080` |
| **JWT Configuration** | | |
| `JWT_SECRET_KEY` | Secret key for JWT signing | (required in production) |
| `JWT_ALGORITHM` | JWT algorithm | `HS256` |
| `JWT_EXPIRATION_HOURS` | Access token expiration | `24` |
| **Email Configuration** | | |
| `SENDGRID_API_KEY` | SendGrid API key | (empty = disabled) |
| `SENDGRID_FROM_EMAIL` | From email address | `noreply@scienthesis.ai` |
| `SENDGRID_FROM_NAME` | From name | `Scienthesis` |
| `APP_BASE_URL` | Application base URL | `https://litscreen-aggregate.preview.emergentagent.com` |

## Development

### Install Dependencies

```bash
pip install -r requirements.txt
```

### Run the Server

The server reads the `PORT` environment variable (defaults to 8080):

```bash
# Using default port 8080
python -m uvicorn server:app --host 0.0.0.0 --port 8080 --reload

# Or with custom port
PORT=8001 python -m uvicorn server:app --host 0.0.0.0 --port $PORT --reload
```

## API Endpoints

### Health & Status

```
GET /health
```

Returns `{"status": "ok"}` with HTTP 200.

```
GET /api/health
```

Returns `{"status": "ok"}` with HTTP 200.

### API Root

```
GET /api/
```

Returns basic API information:

```json
{
  "message": "Scienthesis API",
  "version": "1.0.0"
}
```

### Authentication

#### POST /api/auth/signup

Register a new user.

**Request:**
```json
{
  "email": "user@example.com",
  "password": "SecurePass123!",
  "full_name": "John Doe",
  "timezone": "America/New_York"
}
```

**Password Requirements:**
- At least 8 characters
- At least one uppercase letter
- At least one lowercase letter
- At least one digit
- At least one special character

**Response (201):**
```json
{
  "user_id": "uuid",
  "email": "user@example.com",
  "full_name": "John Doe",
  "is_verified": false,
  "is_active": true,
  "timezone": "America/New_York",
  "created_at": "2025-12-02T01:00:00+00:00",
  "updated_at": "2025-12-02T01:00:00+00:00"
}
```

#### POST /api/auth/login

Authenticate and receive access token.

**Request:**
```json
{
  "email": "user@example.com",
  "password": "SecurePass123!"
}
```

**Response (200):**
```json
{
  "access_token": "eyJhbGc...",
  "token_type": "bearer",
  "user": {
    "user_id": "uuid",
    "email": "user@example.com",
    "full_name": "John Doe",
    "is_verified": false,
    "is_active": true,
    "timezone": "America/New_York",
    "created_at": "2025-12-02T01:00:00+00:00",
    "updated_at": "2025-12-02T01:00:00+00:00"
  }
}
```

#### GET /api/auth/me

Get current user information (requires authentication).

**Headers:**
```
Authorization: Bearer <access_token>
```

**Response (200):**
```json
{
  "user_id": "uuid",
  "email": "user@example.com",
  ...
}
```

#### POST /api/auth/verify-email

Verify user email address.

**Request:**
```json
{
  "token": "verification_token"
}
```

**Response (200):**
```json
{
  "message": "Email verified successfully"
}
```

#### POST /api/auth/resend-verification

Resend verification email (requires authentication).

**Headers:**
```
Authorization: Bearer <access_token>
```

**Response (200):**
```json
{
  "message": "Verification email sent"
}
```

#### POST /api/auth/request-password-reset

Request password reset email.

**Request:**
```json
{
  "email": "user@example.com"
}
```

**Response (200):**
```json
{
  "message": "If the email exists, a password reset link has been sent"
}
```

#### POST /api/auth/reset-password

Reset password using token.

**Request:**
```json
{
  "token": "reset_token",
  "new_password": "NewSecurePass456!"
}
```

**Response (200):**
```json
{
  "message": "Password reset successfully"
}
```

## MongoDB Connection

On startup, the application:
1. Connects to MongoDB using `MONGO_URL`
2. Selects the database specified in `DB_NAME`
3. Pings the database to verify connection
4. Creates indexes:
   - Unique index on `users.email`
   - Unique index on `users.user_id`
5. Logs success or failure

### Collections

#### users
Stores user accounts and authentication data.

**Schema:**
```javascript
{
  user_id: String (UUID, unique),
  email: String (lowercase, unique),
  hashed_password: String (bcrypt),
  full_name: String,
  is_verified: Boolean,
  is_active: Boolean,
  timezone: String (IANA),
  created_at: ISO8601 String,
  updated_at: ISO8601 String
}
```

## Testing

### Stage 1 Tests
```bash
python3 test_stage1.py
```

Tests:
- Health endpoints
- API root
- MongoDB connection

### Stage 2 Tests
```bash
python3 test_stage2.py
```

Tests:
- User signup with validation
- Login and JWT tokens
- Current user endpoint
- Email verification
- Password reset flow
- Invalid token handling

## Deployment Notes

- Do NOT hardcode ports in the application code
- The container orchestration platform will set the `PORT` environment variable
- If `PORT` is not set, the application defaults to 8080
- The application listens on `0.0.0.0` to accept connections from any interface
- CORS is configured to allow the origins specified in `CORS_ORIGINS`
- In production, the platform's supervisor manages the actual port binding (typically 8001)
