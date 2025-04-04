# Streamlit Applications Repository

This repository contains two independent Streamlit applications:

- 📊 `adminDashboard`: An admin-facing dashboard application.
- 🕒 `timecardEntry`: A time tracking and entry application.

Each application is self-contained and connects to a shared PostgreSQL database running inside a Docker container. Setup includes configuring local environments, installing dependencies, and running Streamlit.

---

## 📁 Project Structure

├── adminDashboard/ │ ├── admin.py │ ├── requirements.txt │ ├── .env ← must be created by you │ └── ... │ ├── timecardEntry/ │ ├── app.py │ ├── requirements.txt │ ├── .env ← must be created by you │ └── ... │ └── README.md



---

## ⚙️ Prerequisites

Ensure the following are installed:

- Python 3.8 or higher
- Docker and Docker CLI
- `pip` package manager
- `virtualenv` (`pip install virtualenv`)

---

## 🐘 Step 1: Create PostgreSQL Database Using Docker

1. **Start the PostgreSQL container**:

```
   docker run --name streamlit-postgres \
     -e POSTGRES_DB=your_db_name \
     -e POSTGRES_USER=your_user \
     -e POSTGRES_PASSWORD=your_password \
     -p 5432:5432 \
     -d postgres
```

2. **The DB will be accessible from the host as host.docker.internal (Mac/Windows) or 172.17.0.1 (Linux).
For simplicity, this README uses host.docker.internal.**


---

## 🔐 Step 2: Create .env Files
1. Create a .env file in both adminDashboard/ and timecardEntry/ directories with the following structure:

```
DB_HOST=host.docker.internal
DB_PORT=5432
DB_NAME=your_db_name
DB_USER=your_user
DB_PASSWORD=your_password

EMAIL=youremail@example.com
PASSWORD=your_app_password
ENDPOINT=your local app endpoint
```

💡 Note: Use an app-specific password for your email account.
For Gmail, generate one from https://myaccount.google.com/apppasswords.


---

## 🧭 Step 3: Setup Each Project Individually
You must set up each project in isolation using virtual environments.

# 📊 Setting Up adminDashboard

```
cd adminDashboard
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
streamlit run admin.py
```


---
## ⏱️ Setting Up timecardEntry

```
cd timecardEntry
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

---

## 📌 Notes
 - Both apps must be able to read their .env file—ensure it is created before launching the app.

 - Make sure Docker is running and the PostgreSQL container is active before launching the apps.

 - Use environment variables for all sensitive data—never commit .env files to version control.


---

## 🧹 Optional Cleanup

```
# Stop and remove the database container
docker stop streamlit-postgres
docker rm streamlit-postgres

# Remove virtual environment
rm -rf venv  # macOS/Linux
rmdir /s venv  # Windows (Command Prompt)
```

---

## 📬 Support
  If you run into issues, feel free to open an issue or submit a pull request.