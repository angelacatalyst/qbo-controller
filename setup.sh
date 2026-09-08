#!/bin/bash
# QBO AI Controller Agent — Setup Script
# Usage: chmod +x setup.sh && ./setup.sh

set -e

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║       QBO AI CONTROLLER AGENT — SETUP                       ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | grep -oP '\d+\.\d+')
REQUIRED="3.9"
if [ "$(printf '%s\n' "$REQUIRED" "$PYTHON_VERSION" | sort -V | head -n1)" != "$REQUIRED" ]; then
    echo "❌ Python 3.9+ required. Found: $PYTHON_VERSION"
    exit 1
fi
echo "✅ Python $PYTHON_VERSION detected"

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi

# Activate
source venv/bin/activate
echo "✅ Virtual environment activated"

# Upgrade pip
pip install --upgrade pip --quiet

# Install dependencies
echo "📦 Installing dependencies..."
pip install -r requirements.txt --quiet
echo "✅ Dependencies installed"

# Create .env if not exists
if [ ! -f ".env" ]; then
    echo ""
    echo "⚙️  Creating .env configuration..."
    cp .env.example .env

    # Auto-generate encryption key
    ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

    # Replace placeholders
    sed -i "s|your_fernet_encryption_key_here|$ENCRYPTION_KEY|g" .env
    sed -i "s|your_secret_key_here|$SECRET_KEY|g" .env

    echo "✅ .env created with auto-generated keys"
    echo ""
    echo "⚠️  REQUIRED: Add your QBO credentials to .env:"
    echo "   QBO_CLIENT_ID=your_client_id_from_intuit_developer"
    echo "   QBO_CLIENT_SECRET=your_client_secret_from_intuit_developer"
    echo ""
    echo "   Get credentials at: https://developer.intuit.com"
    echo "   Set redirect URI in Intuit to: http://localhost:8000/qbo/callback"
else
    echo "✅ .env already exists"
fi

# Create static directories
mkdir -p app/static/css app/static/js app/static/img
echo "✅ Static directories created"

# Initialize database
echo "🗄️  Initializing database..."
python3 -c "from app.database import init_db; init_db(); print('✅ Database initialized')"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  ✅ SETUP COMPLETE                                           ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Start the app:  python run.py                               ║"
echo "║  Or:             uvicorn app.main:app --reload               ║"
echo "║  Open:           http://localhost:8000                       ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
