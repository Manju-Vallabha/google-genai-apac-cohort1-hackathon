# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies (build-essential etc. if needed)
# For ADK and standard Python packages, slim usually suffices.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source code
# We copy the productivity_assistant folder and the credentials
COPY productivity_assistant/ ./productivity_assistant/
COPY token.json .

# Copy client.json if it is in the root, but it is inside productivity_assistant
# The agent.py looks for productivity_assistant\client.json by default based on .env
# We should ensure the path matches what's in the env or code.

# Expose the port the app runs on
EXPOSE 8080

# Use the ADK web command to start the server
# We bind to 0.0.0.0 so it's accessible externally in Cloud Run
CMD ["adk", "web"]
