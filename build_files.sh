#!/bin/bash



# Create and activate a virtual environment using python3
python3 -m venv venv
source venv/bin/activate

# Upgrade pip to avoid potential issues
# Upgrade pip
pip install --upgrade pip

# Install dependencies
pip install -r requirements.txt


#
# Collect static files
python manage.py collectstatic --noinput
