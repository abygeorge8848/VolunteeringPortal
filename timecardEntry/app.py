import secrets
import bcrypt
import streamlit as st
import streamlit_authenticator as stauth
from datetime import datetime, timedelta, date
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import os
import yagmail
from io import BytesIO
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

class DatabaseManager:
    """Manages database connections and operations for the volunteer timesheet app."""
    
    def __init__(self):
        """Initialize the database connection pool."""
        # Database connection parameters
        # db_host = os.getenv("DB_HOST", "host.docker.internal")  # Use Docker's internal hostname
        db_host = os.getenv("DB_HOST")
        db_port = os.getenv("DB_PORT")
        db_name = os.getenv("DB_NAME")
        db_user = os.getenv("DB_USER")
        db_password = os.getenv("DB_PASSWORD")

        self.db_params = {
            'dbname': db_name,
            'user': db_user,
            'password': db_password,
            'host': db_host,
            'port': db_port
        }
        
        # Create a connection pool
        self.connection_pool = psycopg2.pool.SimpleConnectionPool(
            1, 10, **self.db_params
        )
        
        # Initialize database if needed
        self.initialize_database()
    
    def get_connection(self):
        """Get a connection from the pool."""
        return self.connection_pool.getconn()
    
    def release_connection(self, conn):
        """Release a connection back to the pool."""
        self.connection_pool.putconn(conn)
    
    def initialize_database(self):
        """Initialize the database tables if they don't exist."""
        conn = self.get_connection()
        try:
            with conn.cursor() as cursor:
                # Check if admins table exists and create if not
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS admins (
                        id SERIAL PRIMARY KEY,
                        name VARCHAR(100) NOT NULL,
                        email VARCHAR(255) UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                
                # Create volunteers table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS volunteers (
                        id SERIAL PRIMARY KEY,
                        volunteer_id VARCHAR(20) UNIQUE,
                        username VARCHAR(100) UNIQUE NOT NULL,
                        name VARCHAR(100) NOT NULL,
                        email VARCHAR(255) UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        date_of_birth DATE,
                        gender VARCHAR(50),
                        father_name VARCHAR(100),
                        profession_or_education VARCHAR(255),
                        college VARCHAR(255),
                        mobile_number VARCHAR(15),
                        address TEXT,
                        reason_to_join TEXT,
                        preferred_joining_date DATE,
                        preferred_working_days TEXT[],      
                        volunteership_type VARCHAR(100),
                        fields_of_interest TEXT[],        
                        other_skills TEXT,
                        previous_experience TEXT,
                        passport_photo BYTEA,
                        aadhar_card_image BYTEA,
                        pan_card_image BYTEA
                    );
                """)
                
                # Create projects table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS projects (
                        id SERIAL PRIMARY KEY,
                        name VARCHAR(255) UNIQUE NOT NULL,
                        created_by INTEGER REFERENCES admins(id),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                
                # Create timesheets table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS timesheets (
                        id SERIAL PRIMARY KEY,
                        volunteer_id INTEGER REFERENCES volunteers(id) ON DELETE CASCADE,
                        project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
                        date DATE NOT NULL,
                        hours DECIMAL(5,2) NOT NULL CHECK (hours > 0),
                        status VARCHAR(50) DEFAULT 'Pending',
                        submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # Create password reset tokens table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS password_reset_tokens (
                        id SERIAL PRIMARY KEY,
                        email VARCHAR(255) UNIQUE NOT NULL REFERENCES volunteers(email) ON DELETE CASCADE,
                        token TEXT NOT NULL,
                        expires_at TIMESTAMP NOT NULL
                    );
                """)
                
                # Insert default projects if they don't exist
                default_projects = [
                    "Community Garden", "Food Bank", "Homeless Shelter", "Youth Mentoring",
                    "Environmental Cleanup", "Senior Center Support", "Local Library", "Animal Shelter"
                ]
                
                for project in default_projects:
                    cursor.execute(
                        "INSERT INTO projects (name) VALUES (%s) ON CONFLICT (name) DO NOTHING;",
                        (project,)
                    )
                
                conn.commit()
        except Exception as e:
            conn.rollback()
            st.error(f"Database initialization error: {e}")
        finally:
            self.release_connection(conn)
    
    def close_all_connections(self):
        """Close all database connections."""
        self.connection_pool.closeall()

    def create_reset_token(self, email):
        """Generate and store a password reset token for the given email."""
        conn = self.get_connection()
        try:
            with conn.cursor() as cursor:
                # Generate a secure random token
                token = secrets.token_urlsafe(32)
                expires_at = datetime.utcnow() + timedelta(hours=1)  # Token expires in 1 hour

                # Insert or update the reset token
                cursor.execute("""
                    INSERT INTO password_reset_tokens (email, token, expires_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (email) DO UPDATE 
                    SET token = EXCLUDED.token, expires_at = EXCLUDED.expires_at;
                """, (email, token, expires_at))

                conn.commit()
                return token  # Return the token for sending in the email
        except Exception as e:
            conn.rollback()
            st.error(f"Error creating reset token: {e}")
        finally:
            self.release_connection(conn)


class VolunteerTimesheet:
    def __init__(self):
        # Initialize database manager
        self.db_manager = DatabaseManager()
        
        # Initialize credentials and config
        self.credentials = {'usernames': {}}
        self.cookie_config = {
            'cookie_name': 'volunteer_app_cookie',
            'key': self.generate_secret_key(),
            'expiry_days': 30
        }
        
        # Load credentials
        self.load_credentials_from_db()

        # Initialize authenticator
        self.authenticator = stauth.Authenticate(
            self.credentials,
            self.cookie_config['cookie_name'],
            self.cookie_config['key'],
            self.cookie_config['expiry_days']
        )

        # Ensure session state is initialized correctly
        if "authentication_status" not in st.session_state:
            st.session_state["authentication_status"] = None

        if "username" not in st.session_state:
            st.session_state["username"] = None

        # Initialize session state for current week and timesheet
        if "current_week" not in st.session_state:
            st.session_state.current_week = datetime.now() - timedelta(days=datetime.now().weekday())

        if "timesheet_df" not in st.session_state:
            st.session_state.timesheet_df = self.create_timesheet_dataframe()

        # Load projects from database
        self.projects = self.load_projects_from_db()

    def generate_secret_key(self):
        """Generate a secret key for cookie encryption."""
        return secrets.token_hex(32)  # Generates a secure random key
    
    def load_credentials_from_db(self):
        """Load user credentials from the database."""
        conn = self.db_manager.get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT id, username, name, email, password_hash FROM volunteers;")
                volunteers = cursor.fetchall()
                
                # Format credentials for the authenticator
                self.credentials = {'usernames': {}}
                for volunteer in volunteers:
                    self.credentials['usernames'][volunteer['username']] = {
                        'name': volunteer['name'],
                        'email': volunteer['email'],
                        'password': volunteer['password_hash'],
                        'id': volunteer['id']
                    }
        except Exception as e:
            st.error(f"Error loading credentials: {e}")
        finally:
            self.db_manager.release_connection(conn)
    
    def load_projects_from_db(self):
        """Load projects from the database."""
        conn = self.db_manager.get_connection()
        projects = []
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT name FROM projects ORDER BY name")
                project_rows = cursor.fetchall()
                projects = [row[0] for row in project_rows]
        except Exception as e:
            st.error(f"Error loading projects: {e}")
            # Fallback to default projects
            projects = [
                "Community Garden", "Food Bank", "Homeless Shelter", "Youth Mentoring",
                "Environmental Cleanup", "Senior Center Support", "Local Library", "Animal Shelter"
            ]
        finally:
            self.db_manager.release_connection(conn)
        return projects
    
    def register_user(self, name, email, username, password, extra):
        """Register a new user in the database with optional extended fields."""
        conn = self.db_manager.get_connection()
        try:
            with conn.cursor() as cursor:
                # Check if username already exists
                cursor.execute("SELECT 1 FROM volunteers WHERE username = %s", (username,))
                if cursor.fetchone():
                    return False, "Username already exists"

                # Check if email already exists
                cursor.execute("SELECT 1 FROM volunteers WHERE email = %s", (email,))
                if cursor.fetchone():
                    return False, "Email already exists"

                # Hash the password
                hashed_password = stauth.Hasher.hash(password)

                # Prepare insert query with all optional fields
                cursor.execute("""
                    INSERT INTO volunteers (
                        username, name, email, password_hash,
                        date_of_birth, gender, father_name, profession_or_education,
                        college, mobile_number, address, reason_to_join,
                        preferred_joining_date, preferred_working_days, volunteership_type,
                        fields_of_interest, other_skills, previous_experience,
                        passport_photo, aadhar_card_image, pan_card_image
                    )
                    VALUES (%s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s)
                """, (
                    username,
                    name,
                    email,
                    hashed_password,
                    extra.get("dob"),
                    extra.get("gender"),
                    extra.get("father_name"),
                    extra.get("profession_or_education"),
                    extra.get("college"),
                    extra.get("mobile_number"),
                    extra.get("address"),
                    extra.get("reason_to_join"),
                    extra.get("preferred_joining_date"),
                    extra.get("preferred_days"),
                    extra.get("vol_type"),
                    extra.get("fields_of_interest"),
                    extra.get("skills"),
                    extra.get("experience"),
                    extra.get("passport_photo"),
                    extra.get("aadhar"),
                    extra.get("pan")
                ))

                cursor.execute("SELECT currval(pg_get_serial_sequence('volunteers', 'id'))")

                # Get the new user's ID
                user_id = cursor.fetchone()[0]

                # Generate volunteer_id like mima0001
                volunteer_id = f"mima{user_id:06d}"

                print(f"The volunteer_id : {volunteer_id}")

                # Update the volunteer_id for the newly inserted user
                cursor.execute("""
                    UPDATE volunteers SET volunteer_id = %s WHERE id = %s
                """, (volunteer_id, user_id))

                conn.commit()
                self.load_credentials_from_db()
                return True, f"Registration successful. Your Volunteer ID is {volunteer_id}"
                # return True, f"Registration successful."

        except Exception as e:
            conn.rollback()
            return False, f"Registration error: {e}"
        finally:
            self.db_manager.release_connection(conn)



    def render_authentication(self):
        """Render authentication page with login and registration options."""
        st.title("MIMA Volunteer's Timesheet")

        # Create tabs for login and registration
        login_tab, register_tab = st.tabs(["Login", "Register"])

        with login_tab:
            # Attempt login
            login_result = self.authenticator.login(location='main')
            # Ensure we handle None correctly
            if login_result is not None:
                name, authentication_status, username = login_result
            else:
                authentication_status = None
                username = None  # Handle None username case
    
            if authentication_status:
                st.session_state['authentication_status'] = authentication_status
                st.write(f"Welcome *{name}*!")
                st.session_state.username = username
                
                # Get volunteer ID and store in session state
                conn = self.db_manager.get_connection()
                try:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT id FROM volunteers WHERE username = %s", (username,))
                        result = cursor.fetchone()
                        if result:
                            st.session_state.volunteer_id = result[0]
                finally:
                    self.db_manager.release_connection(conn)
                
                self.render()
            else:
                if st.session_state.get('FormSubmitter:Login-Login', False):
                    st.error('Incorrect username or password. Please try again.')
                else:
                    st.warning('Please enter your username and password.')

            if st.button("Forgot Password?"):
                st.session_state["reset_password"] = True
                st.rerun()

        with register_tab:
            try:
                registration_success = st.session_state.get('registration_success', False)
                if registration_success:
                    st.success('Registration successful! Please log in.')
                    
                st.markdown("### Guidelines for Registration")
                st.info("""
                    ### 📝 Volunteer Registration Form

                    This is a form for the registration process to be started.  
                    **One-time Registration Fee:** ₹200/-

                    ---

                    ### 📌 Types of Volunteer-ship

                    **Offline**
                    - Event Volunteer-ship
                    - Long Term: 3 months / 6 months / 1 year

                    **Online**
                    - 80 hours
                    - 120 hours

                    ---

                    ### ✅ Eligibility

                    - Association should be continuous.
                    - Assigned work must be completed within the stipulated time.
                    - Certificate will be approved by the Department Head.

                    ---

                    ### 🎖️ Rewards

                    - Certificate immediately after completion of the event.
                    - Certificates for:
                      - 3 months, 6 months & 1 year (offline)
                      - 80 or 120 hours (online)
                    - Appreciation for 6 months & 1 year service.
                    - Letter of Recommendation after completing 1 year.
                    - Eligibility for promotion to **Volunteer Coordinator** after 1 year, if continuing.

                    ---

                    ### 📎 Guidelines

                    - Always coordinate directly with the designated person.
                    - Communication is mandatory in case of delays or before taking any leave/gap.
                    - Attendance in all meetings organized by **MIMA** or the **Department Head** is compulsory.

                """)

                # Required fields
                reg_name = st.text_input('Name*', key='reg_name')
                reg_email = st.text_input('Email*', key='reg_email')
                reg_username = st.text_input('Username*', key='reg_username')
                reg_password = st.text_input('Password*', type='password', key='reg_password')
                reg_password_confirm = st.text_input('Confirm Password*', type='password', key='reg_password_confirm')

                # Optional fields
                dob = st.date_input('Date of Birth', key='reg_dob')
                gender = st.selectbox('Gender', ['male', 'female', 'prefer not to say', 'other'], key='reg_gender')
                if gender == 'other':
                    gender = st.text_input("Please specify your gender", key='reg_gender_other') or 'other'

                father_name = st.text_input("Father's Name", key='reg_father_name')
                profession_or_education = st.text_input("Profession / Educational Qualification", key='reg_profession')
                college = st.text_input("College", key='reg_college')
                mobile_number = st.text_input("Mobile Number", key='reg_mobile')
                address = st.text_area("Address", key='reg_address')
                reason_to_join = st.text_area("Why do you want to join the organization?", key='reg_reason')
                preferred_joining_date = st.date_input("Preferred Date of Joining", key='reg_join_date')

                preferred_days = st.multiselect("Preferred Days of Working", [
                    'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'
                ], key='reg_work_days')

                volunteership_type = st.radio("Type of Volunteership", [
                    'Event Volunteership (offline)', '3 Months (offline)', '6 months (offline)', '1 Year (offline)', '80 Hours (online)', '120 Hours (online)'
                ], key='reg_vol_type')

                fields_of_interest = st.multiselect("Field(s) of Interest", [
                    'Digital  marketing', 'Fundraiser', 'Data entry', 'Event creator', 'Report writing', 'Networking', 'Public relation', 'Teaching', 'Event management', 
                    'Content Writer', 'Research and Development', 'Others'
                ], key='reg_interests')

                other_skills = st.text_area("Other Skills", key='reg_skills')
                previous_experience = st.text_area("Any Previous Experience in social sector. (NA in case of no experience)", key='reg_experience')

                passport_photo = st.file_uploader("Upload Passport Size Photo", type=['jpg', 'jpeg', 'png'], key='reg_passport')
                aadhar_card = st.file_uploader("Upload Aadhaar Card", type=['jpg', 'jpeg', 'png', 'pdf'], key='reg_aadhar')
                pan_card = st.file_uploader("Upload PAN Card", type=['jpg', 'jpeg', 'png', 'pdf'], key='reg_pan')

                if registration_success:
                    st.success('Registration successful! Please log in.')
                    st.session_state.registration_success = False

                if st.button('Register'):
                    if reg_password != reg_password_confirm:
                        st.error('Passwords do not match')
                    elif not all([reg_name, reg_email, reg_username, reg_password]):
                        st.error("Please fill in all required fields")
                    else:
                        # Prepare optional values (convert to None if empty)
                        values = {
                            "dob": dob if dob else None,
                            "gender": gender,
                            "father_name": father_name or None,
                            "profession_or_education": profession_or_education or None,
                            "college": college or None,
                            "mobile_number": mobile_number or None,
                            "address": address or None,
                            "reason_to_join": reason_to_join or None,
                            "preferred_joining_date": preferred_joining_date if preferred_joining_date else None,
                            "preferred_days": preferred_days if preferred_days else None,
                            "vol_type": volunteership_type,
                            "fields_of_interest": fields_of_interest if fields_of_interest else None,
                            "skills": other_skills or None,
                            "experience": previous_experience or None,
                            "passport_photo": passport_photo.read() if passport_photo else None,
                            "aadhar": aadhar_card.read() if aadhar_card else None,
                            "pan": pan_card.read() if pan_card else None
                        }

                        # Pass everything to your registration function
                        success, message = self.register_user(
                            name=reg_name,
                            email=reg_email,
                            username=reg_username,
                            password=reg_password,
                            extra=values
                        )

                        if success:
                            st.session_state.registration_success = True
                            st.rerun()
                        else:
                            st.error(message)

            except Exception as e:
                st.error(f'An error occurred: {e}')


    def render_password_reset(self):
        """Render password reset form."""
        st.title("Reset Password")

        token = st.query_params.get("reset_token", [None])
        print(f"The token is : {token}")

        if token[0]:
            new_password = st.text_input("New Password", type="password")
            confirm_password = st.text_input("Confirm Password", type="password")

            if st.button("Reset Password"):
                if new_password != confirm_password:
                    st.error("Passwords do not match!")
                else:
                    success, message = self.reset_password(token, new_password)
                    if success:
                        st.success("Password successfully reset! You can now log in.")
                        st.session_state.pop("reset_password", None)
                    else:
                        st.error(message)
        else:
            email = st.text_input("Enter your registered email")
            if st.button("Send Reset Link"):
                success, message = self.send_reset_email(email)
                if success:
                    st.success("A password reset link has been sent to your email.")
                else:
                    st.error(message)

        
        st.button("Back to Login", on_click = self.clear_reset_state)

    
    def clear_reset_state(self):
        st.session_state.pop("reset_password", None)
        st.session_state.pop("reset_token", None)
        st.query_params.clear()
        st.session_state["reroute_to_login"] = True  # Signal rerun outside
        
        

    def send_reset_email(self, email):
        """Send password reset email with a unique token link."""
        conn = self.db_manager.get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT username FROM volunteers WHERE email = %s", (email,))
                user = cursor.fetchone()
                print(f"The user is : {user}")

                if not user:
                    return False, "Email not found in our records."

            # Generate and store a token
            token = self.db_manager.create_reset_token(email)
            endpoint = os.getenv("ENDPOINT")
            reset_link = f"{endpoint}/?reset_token={token}"

            server_email = os.getenv("EMAIL")
            password = os.getenv("PASSWORD")

            print(f"Email is : {server_email}")
            print(f"Password is : {password}")
            print(f"Reset link is : {reset_link}")

            # Send the email
            yag = yagmail.SMTP(server_email, password)
            subject = "Password Reset Request"
            body = f"Click the following link to reset your password: {reset_link}"
            yag.send(email, subject, body)

            return True, "Reset link sent."
        except Exception as e:
            return False, f"Error sending email: {e}"
        finally:
            self.db_manager.release_connection(conn)

    
    def reset_password(self, token, new_password):
        """Reset password using a token."""
        conn = self.db_manager.get_connection()
        try:
            with conn.cursor() as cursor:
                # Check if the token is valid and not expired
                cursor.execute("SELECT email FROM password_reset_tokens WHERE token = %s AND expires_at > NOW()", (token,))
                result = cursor.fetchone()

                if not result:
                    return False, "Invalid or expired token."

                email = result[0]
                # hashed_password = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt())
                hashed_password = stauth.Hasher.hash(new_password)

                # Update user's password
                cursor.execute("""
                    UPDATE volunteers 
                    SET password_hash = %s 
                    WHERE email = %s
                """, (hashed_password, email))

                # Delete the used token
                cursor.execute("DELETE FROM password_reset_tokens WHERE email = %s", (email,))

                conn.commit()

                # ✅ Clear session state for reset process
                st.session_state.pop("reset_password", None)
                st.session_state.pop("reset_token", None)

                return True, "Password successfully reset."
        finally:
            self.db_manager.release_connection(conn)


    def logout_button(self):
        """Add logout button to the sidebar."""
        if st.session_state.get("authentication_status"):
            # Try different logout method calls
            try:
                self.authenticator.logout('Logout')
            except (TypeError, ValueError):
                try:
                    self.authenticator.logout(button_name='Logout', location='sidebar')
                except (TypeError, ValueError):
                    self.authenticator.logout('sidebar', 'Logout')


    def get_week_dates(self):
        """Returns a list of dates (Sunday to Saturday) for the current week."""
        start_of_week = st.session_state.current_week
        return [(start_of_week + timedelta(days=i)) for i in range(7)]

    def create_timesheet_dataframe(self) -> pd.DataFrame:
        """
        Creates or loads the timesheet dataframe for the current user and week.
        """
        # Get list of dates for the current week
        dates = self.get_week_dates()
        
        # Build initial columns for display
        day_columns = [
            f"{date.strftime('%A')}\n{date.strftime('%m/%d')}" for date in dates
        ]

        # Build a blank DataFrame with 5 rows for projects
        df = pd.DataFrame(
            np.zeros((5, len(day_columns)), dtype=float),
            columns=day_columns
        )
        df.insert(0, "Project", [""] * 5)

        # If user is authenticated, try to load from database
        if st.session_state.get('authentication_status') and hasattr(st.session_state, 'volunteer_id'):
            conn = self.db_manager.get_connection()
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    # Get start and end dates for the week
                    start_date = dates[0].strftime('%Y-%m-%d')
                    end_date = dates[-1].strftime('%Y-%m-%d')
                    
                    # Query timesheet entries for this volunteer and week
                    cursor.execute("""
                        SELECT t.date, p.name as project, t.hours 
                        FROM timesheets t
                        JOIN projects p ON t.project_id = p.id
                        WHERE t.volunteer_id = %s AND t.date BETWEEN %s AND %s
                    """, (st.session_state.volunteer_id, start_date, end_date))
                    
                    timesheet_entries = cursor.fetchall()
                    
                    if timesheet_entries:
                        # Convert to DataFrame
                        entries_df = pd.DataFrame(timesheet_entries)

                        # Convert hours to numeric to ensure summation works correctly
                        entries_df['hours'] = pd.to_numeric(entries_df['hours'], errors='coerce')
                        
                        # Format the day-date column like "Monday\n03/06"
                        entries_df['day_date'] = entries_df['date'].apply(
                            lambda x: f"{x.strftime('%A')}\n{x.strftime('%m/%d')}" 
                            if isinstance(x, (datetime, date)) else f"{datetime.strptime(x, '%Y-%m-%d').strftime('%A')}\n{datetime.strptime(x, '%Y-%m-%d').strftime('%m/%d')}"
                        )
                        
                        # Pivot to get the same format as our display DataFrame
                        pivoted = entries_df.pivot_table(
                            index="project",
                            columns="day_date",
                            values="hours",
                            aggfunc="sum",
                            fill_value=0
                        ).reset_index()
                        
                        # Reindex to ensure all day_columns exist
                        for col in day_columns:
                            if col not in pivoted.columns:
                                pivoted[col] = 0
                        
                        # Ensure the columns are in the right order
                        pivoted = pivoted[["project"] + day_columns]
                        
                        # Merge with our blank template
                        # First, get the projects that already have data
                        existing_projects = pivoted["project"].tolist()
                        
                        # Then filter out rows in df that would be duplicated
                        template_rows = df[~df["Project"].isin(existing_projects)]
                        
                        # Rename pivoted column to match template
                        pivoted = pivoted.rename(columns={"project": "Project"})
                        
                        # Concatenate the DataFrames
                        result_df = pd.concat([pivoted, template_rows])
                        
                        # Ensure we don't exceed 5 rows total
                        if len(result_df) > 5:
                            result_df = result_df.head(5)
                        
                        return result_df
            except Exception as e:
                st.error(f"Error loading timesheet data: {e}")
            finally:
                self.db_manager.release_connection(conn)

        return df

    def render(self):
        """Renders the entire timesheet page UI."""
        # Add logout to sidebar
        self.logout_button()
        
        st.title(f"Volunteer Timesheet - {st.session_state['name']}")

        conn = self.db_manager.get_connection()
        username = st.session_state.username
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id FROM volunteers WHERE username = %s", (username,))
                result = cursor.fetchone()
                if result:
                    st.session_state.volunteer_id = result[0]
        finally:
            self.db_manager.release_connection(conn)

        # Week Navigation Controls with Fixed Button Sizes
        nav_col1, nav_col2, nav_col3 = st.columns([2, 6, 2])
        with nav_col1:
            if st.button("◀ Prev", key="prev_week", help="Go to Previous Week"):
                st.session_state.current_week -= timedelta(weeks=1)
                st.session_state.timesheet_df = self.create_timesheet_dataframe()

        with nav_col2:
            st.markdown(f"<h4 style='text-align:center;'>Week of {st.session_state.current_week.strftime('%m/%d/%Y')}</h4>", unsafe_allow_html=True)

        with nav_col3:
            if st.button("Next ▶", key="next_week", help="Go to Next Week"):
                st.session_state.current_week += timedelta(weeks=1)
                st.session_state.timesheet_df = self.create_timesheet_dataframe()

        # Tabs for Time Entry and Statistics
        tab1, tab2, tab3 = st.tabs(["🕒 Time Entry", "📊 Statistics", "👤 Profile"])


        with tab1:
            self.render_time_entry()

        with tab2:
            self.render_statistics(username)
        
        with tab3:
            self.render_profile(username)
            

     
    def render_profile(self, username):
        conn = self.db_manager.get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT
                        volunteer_id, name, email, username, date_of_birth, gender, father_name,
                        profession_or_education, college, mobile_number, address,
                        reason_to_join, preferred_joining_date, preferred_working_days,
                        volunteership_type, fields_of_interest, other_skills,
                        previous_experience, passport_photo, aadhar_card_image, pan_card_image
                    FROM volunteers
                    WHERE username = %s
                """, (username,))
                profile = cursor.fetchone()
    
                if not profile:
                    st.error("Profile data not found.")
                    return
    
                # Unpack and prepare data
                (
                    volunteer_id, name, email, username_db, dob, gender, father_name, profession, college,
                    mobile, address, reason, joining_date, work_days, vol_type, interest_fields,
                    skills, experience, passport_img, aadhar_img, pan_img
                ) = profile
    
                with st.form("update_profile_form"):
                    st.subheader("Update Your Profile")
    
                    st.text_input("Volunteer ID", value=volunteer_id, disabled=True)
                    name = st.text_input("Name", value=name)
                    email = st.text_input("Email", value=email)
                    dob = st.date_input("Date of Birth", value=dob) if dob else st.date_input("Date of Birth")
                    gender = st.selectbox("Gender", ["Male", "Female", "Prefer not to say", "Other"], index=["Male", "Female", "Prefer not to say", "Other"].index(gender) if gender in ["Male", "Female", "Prefer not to say", "Other"] else 3)
                    if gender == "Other":
                        gender = st.text_input("Specify Gender", value=gender)
    
                    father_name = st.text_input("Father's Name", value=father_name or "")
                    profession = st.text_input("Profession / Education", value=profession or "")
                    college = st.text_input("College", value=college or "")
                    mobile = st.text_input("Mobile Number", value=mobile or "")
                    address = st.text_area("Address", value=address or "")
                    reason = st.text_area("Reason to Join", value=reason or "")
                    joining_date = st.date_input("Preferred Joining Date", value=joining_date) if joining_date else st.date_input("Preferred Joining Date")
                
                    days_selected = st.multiselect(
                        "Preferred Working Days",
                        options=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
                        default = work_days if work_days else []
                    )
                    vol_type = st.radio(
                        "Type of Volunteership",
                        ['Event Volunteership (offline)', '3 Months (offline)', '6 months (offline)', '1 Year (offline)', '80 Hours (online)', '120 Hours (online)'],
                        index=['Event Volunteership (offline)', '3 Months (offline)', '6 months (offline)', '1 Year (offline)', '80 Hours (online)', '120 Hours (online)'].index(vol_type) if vol_type else 0
                    )
                    interest_fields = st.multiselect(
                        "Fields of Interest",
                        options=['Digital  marketing', 'Fundraiser', 'Data entry', 'Event creator', 'Report writing', 'Networking', 'Public relation', 'Teaching', 'Event management', 
                'Content Writer', 'Research and Development', 'Others'],
                        default = interest_fields if interest_fields else []
                    )
                    skills = st.text_input("Other Skills", value=skills or "")
                    experience = st.text_area("Previous Experience", value=experience or "")
    
                    st.markdown("#### Upload Documents")
                    
                    def show_upload(label, file_blob):
                        if file_blob:
                            try:
                                st.image(BytesIO(file_blob), width=200, caption=f"Existing {label}")
                            except Exception:
                                st.warning(f"{label}: Cannot preview image. It may be corrupted or in an unsupported format.")
                        else:
                            st.markdown(f"**{label}:** _No document uploaded_")
                        return st.file_uploader(f"Upload New {label}", type=["jpg", "jpeg", "png"], key=label)
                    
                    passport_img_new = show_upload("Passport Size Photo", passport_img)
                    aadhar_img_new = show_upload("Aadhar Card", aadhar_img)
                    pan_img_new = show_upload("PAN Card", pan_img)
    
                    if st.form_submit_button("Save Profile"):
                        try:
                            cursor.execute("""
                                UPDATE volunteers
                                SET
                                    name = %s,
                                    email = %s,
                                    date_of_birth = %s,
                                    gender = %s,
                                    father_name = %s,
                                    profession_or_education = %s,
                                    college = %s,
                                    mobile_number = %s,
                                    address = %s,
                                    reason_to_join = %s,
                                    preferred_joining_date = %s,
                                    preferred_working_days = %s,
                                    volunteership_type = %s,
                                    fields_of_interest = %s,
                                    other_skills = %s,
                                    previous_experience = %s,
                                    passport_photo = %s,
                                    aadhar_card_image = %s,
                                    pan_card_image = %s
                                WHERE username = %s
                            """, (
                                name,
                                email,
                                dob,
                                gender,
                                father_name,
                                profession,
                                college,
                                mobile,
                                address,
                                reason,
                                joining_date,
                                days_selected,
                                vol_type,
                                interest_fields,
                                skills,
                                experience,
                                passport_img_new.read() if passport_img_new is not None else bytes(profile[17]),
                                aadhar_img_new.read() if aadhar_img_new is not None else bytes(profile[18]),
                                pan_img_new.read() if pan_img_new is not None else bytes(profile[19]),
                                username
                            ))
                            conn.commit()
                            st.success("Profile updated successfully!")
    
                            # Optional: update session state if name/email changed
                            st.session_state['name'] = name
                            st.session_state['email'] = email
                        except Exception as e:
                            conn.rollback()
                            st.error(f"Error updating profile: {e}")
        finally:
            self.db_manager.release_connection(conn)


    def render_time_entry(self):
        """Renders the time entry grid with Save and Submit functionality."""
        st.subheader("Enter Volunteer Hours")

        # Load existing data if available
        edited_df = st.data_editor(
            st.session_state.timesheet_df,
            column_config={
                "Project": st.column_config.SelectboxColumn(
                    "Project",
                    options=self.projects,
                    required=True
                )
            },
            num_rows="dynamic",
            hide_index=True,
            use_container_width=True
        )

        # Calculate and display daily & weekly totals
        daily_totals = edited_df.iloc[:, 1:].sum()
        weekly_total = daily_totals.sum()

        # Display total row
        total_row = pd.DataFrame(
            [["Total"] + list(daily_totals)] if not daily_totals.empty else [],
            columns=st.session_state.timesheet_df.columns
        )
        st.dataframe(total_row, hide_index=True, use_container_width=True)

        st.write(f"### Total Hours This Week: {weekly_total:.1f}")

        # Save and Submit Buttons
        _, col_save, col_submit = st.columns([6, 2, 2])
        with col_save:
            if st.button("💾 Save", key="save_timesheet", help="Save progress without submission"):
                st.session_state.timesheet_df = edited_df
                self.save_to_database(edited_df, "Saved")
                st.success("Timesheet saved successfully!")

        with col_submit:
            if st.button("✅ Submit", key="submit_timesheet", help="Finalize and submit your timesheet"):
                self.save_to_database(edited_df, "Pending")
                st.success("Timesheet submitted successfully!")

    def save_to_database(self, df: pd.DataFrame, status: str = "Pending") -> None:
        """Saves the timesheet data to the database."""
        if not hasattr(st.session_state, 'volunteer_id'):
            st.error("User ID not found. Please log in again.")
            return
        
        # Get the dates for the current week
        dates = self.get_week_dates()
        day_date_mapping = {
            f"{date.strftime('%A')}\n{date.strftime('%m/%d')}": date 
            for date in dates
        }
        
        # Convert wide DataFrame to long form
        melted_df = df.melt(
            id_vars=["Project"],
            var_name="DayDate",
            value_name="Hours"
        ).copy()
        
        # Filter out rows where Hours is 0 or Project is empty
        melted_df = melted_df[(melted_df["Hours"] > 0) & (melted_df["Project"] != "")].dropna(subset=["Hours"])
        
        if melted_df.empty:
            st.info("No hours to save.")
            return
        
        # Map day names to actual dates
        melted_df["Date"] = melted_df["DayDate"].map(day_date_mapping)
        
        conn = self.db_manager.get_connection()
        try:
            with conn.cursor() as cursor:
                # Begin transaction
                conn.autocommit = False
                
                # For each entry, we need to:
                # 1. Find the project_id
                # 2. Insert or update the timesheet entry
                for _, row in melted_df.iterrows():
                    # Get project_id
                    cursor.execute("SELECT id FROM projects WHERE name = %s", (row["Project"],))
                    project_result = cursor.fetchone()
                    
                    if not project_result:
                        # Create project if it doesn't exist
                        cursor.execute("INSERT INTO projects (name) VALUES (%s) RETURNING id", (row["Project"],))
                        project_id = cursor.fetchone()[0]
                    else:
                        project_id = project_result[0]
                    
                    # Format date for database
                    date_str = row["Date"].strftime("%Y-%m-%d")
                    
                    # Check if entry already exists
                    cursor.execute(
                        "SELECT id FROM timesheets WHERE volunteer_id = %s AND project_id = %s AND date = %s",
                        (st.session_state.volunteer_id, project_id, date_str)
                    )
                    timesheet_result = cursor.fetchone()
                    
                    if timesheet_result:
                        # Update existing entry
                        cursor.execute(
                            "UPDATE timesheets SET hours = %s, status = %s, submitted_at = NOW() WHERE id = %s",
                            (float(row["Hours"]), status, timesheet_result[0])
                        )
                    else:
                        # Insert new entry
                        cursor.execute(
                            """
                            INSERT INTO timesheets (volunteer_id, project_id, date, hours, status)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            (st.session_state.volunteer_id, project_id, date_str, float(row["Hours"]), status)
                        )
                
                # Commit transaction
                conn.commit()
            
        except Exception as e:
            conn.rollback()
            st.error(f"Error saving timesheet: {e}")
        finally:
            conn.autocommit = True
            self.db_manager.release_connection(conn)

    def render_statistics(self, username: str) -> None:
        """
        Renders personal volunteer statistics for the logged-in user.
        Filters and visualizes only their own timesheet data.
        """
        st.header("Your Volunteer Statistics")

        conn = self.db_manager.get_connection()
        try:
            with conn.cursor() as cursor:
                # Get volunteer's internal ID and volunteer_id string
                cursor.execute("SELECT id, volunteer_id FROM volunteers WHERE username = %s", (username,))
                result = cursor.fetchone()
                if not result:
                    st.error("User not found.")
                    return

                volunteer_internal_id, volunteer_id_str = result

                # Total Volunteer Hours
                cursor.execute("SELECT SUM(hours) FROM timesheets WHERE status = 'Approved' AND volunteer_id = %s", (volunteer_internal_id,))
                total_hours = cursor.fetchone()[0] or 0

                # Most Active Project
                cursor.execute("""
                    SELECT p.name, SUM(t.hours) as total_hours
                    FROM timesheets t
                    JOIN projects p ON t.project_id = p.id
                    WHERE t.status = 'Approved' AND t.volunteer_id = %s
                    GROUP BY p.name
                    ORDER BY total_hours DESC
                    LIMIT 1
                """, (volunteer_internal_id,))
                result = cursor.fetchone()
                most_active_project = result[0] if result else "N/A"

                # Number of Weeks Active
                cursor.execute("SELECT MIN(date), MAX(date) FROM timesheets WHERE volunteer_id = %s", (volunteer_internal_id,))
                result = cursor.fetchone()
                if result[0] and result[1]:
                    min_date, max_date = result[0], result[1]
                    num_weeks = max((max_date - min_date).days // 7, 1)
                else:
                    num_weeks = 1

                avg_weekly_hours = total_hours / num_weeks

                # Number of unique projects
                cursor.execute("SELECT COUNT(DISTINCT project_id) FROM timesheets WHERE volunteer_id = %s", (volunteer_internal_id,))
                unique_projects = cursor.fetchone()[0]

            # Daily trend of volunteer hours (last 30 days)
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT date, SUM(hours) as total_hours
                    FROM timesheets
                    WHERE volunteer_id = %s
                    GROUP BY date
                    ORDER BY date DESC
                    LIMIT 30
                """, (volunteer_internal_id,))
                daily_hours_data = cursor.fetchall()
                daily_hours = pd.DataFrame(daily_hours_data)
                if not daily_hours.empty and 'date' in daily_hours.columns:
                    daily_hours = daily_hours.sort_values(by="date")

            # Weekly trend (last 30 weeks)
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT DATE_TRUNC('week', date) AS week, SUM(hours) as total_hours
                    FROM timesheets
                    WHERE status = 'Approved' AND volunteer_id = %s
                    GROUP BY week
                    ORDER BY week DESC
                    LIMIT 30
                """, (volunteer_internal_id,))
                weekly_hours_data = cursor.fetchall()
                weekly_hours = pd.DataFrame(weekly_hours_data)
                if not weekly_hours.empty and 'week' in weekly_hours.columns:
                    weekly_hours = weekly_hours.sort_values(by="week")

            # Project-wise distribution
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT p.name, SUM(t.hours) as total_hours
                    FROM timesheets t
                    JOIN projects p ON t.project_id = p.id
                    WHERE t.status = 'Approved' AND t.volunteer_id = %s
                    GROUP BY p.name
                    ORDER BY total_hours DESC
                """, (volunteer_internal_id,))
                project_data = cursor.fetchall()
                project_totals = pd.DataFrame(project_data)

            # Key Metrics
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Total Hours", f"{total_hours:.1f}")
                st.metric("Most Active Project", most_active_project)
            with col2:
                st.metric("Avg Weekly Hours", f"{avg_weekly_hours:.1f}")
                st.metric("Projects Involved", f"{unique_projects}")
            with col3:
                st.metric("Weeks Active", f"{num_weeks}")
                st.metric("Volunteer ID", volunteer_id_str)

            # Charts
            st.subheader("📊 Your Contribution by Project")
            if not project_totals.empty:
                fig, ax = plt.subplots()
                project_totals['total_hours'] = pd.to_numeric(project_totals['total_hours'], errors='coerce')
                project_totals.set_index('name')['total_hours'].plot(kind="pie", autopct="%1.1f%%", ax=ax)
                ax.set_ylabel("")
                st.pyplot(fig)
            else:
                st.info("No project data available.")

            st.subheader("📅 Daily Hours (Last 30 Days)")
            if not daily_hours.empty:
                fig, ax = plt.subplots()
                daily_hours['total_hours'] = pd.to_numeric(daily_hours['total_hours'], errors='coerce')
                daily_hours.set_index('date')['total_hours'].plot(kind="bar", ax=ax, color="skyblue")
                ax.set_ylabel("Total Hours")
                ax.set_xlabel("Date")
                plt.xticks(rotation=45)
                st.pyplot(fig)
            else:
                st.info("No daily data.")

            st.subheader("📈 Weekly Hours (Last 30 Weeks)")
            if not weekly_hours.empty:
                fig, ax = plt.subplots()
                weekly_hours['total_hours'] = pd.to_numeric(weekly_hours['total_hours'], errors='coerce')
                weekly_hours.set_index('week')['total_hours'].plot(kind="line", marker="o", linestyle="-", ax=ax, color="green")
                ax.set_ylabel("Total Hours")
                ax.set_xlabel("Week")
                plt.xticks(rotation=45)
                st.pyplot(fig)
            else:
                st.info("No weekly data.")

        except Exception as e:
            st.error(f"Error retrieving statistics: {e}")
        finally:
            self.db_manager.release_connection(conn)


def hash_function(password: str) -> str:
    """Hash a password using bcrypt."""
    salt = bcrypt.gensalt()
    hashed_password = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed_password.decode("utf-8")

def verify_password(password: str, hashed_password: str) -> bool:
    """Verify a password against its hashed version."""
    return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))



def main():
    st.set_page_config(page_title="MIMA Volunteer Timesheet", page_icon="🕒")

    # Initialize the timesheet application
    timesheet = VolunteerTimesheet()

    query_params = st.query_params
    if "reset_token" in query_params and "reset_password" not in st.session_state:
        st.session_state["reset_token"] = query_params["reset_token"]
        st.session_state["reset_password"] = True
        st.rerun()  # Set state and rerun to render the correct page

    if st.session_state.get("reroute_to_login"):
        st.session_state.pop("reroute_to_login")
        st.rerun()

    # Ensure session state is initialized correctly
    if "authentication_status" not in st.session_state:
        st.session_state["authentication_status"] = None  # Initialize properly

    if "username" not in st.session_state:
        st.session_state["username"] = None

    # **Restore session from authenticator cookies if available**
    if st.session_state["authentication_status"] is None:
        try:
            name, authentication_status, username = timesheet.authenticator.login("silent")

            if authentication_status:
                # Get volunteer ID
                conn = timesheet.db_manager.get_connection()
                try:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT id FROM volunteers WHERE username = %s", (username,))
                        result = cursor.fetchone()
                        print(f"The volunteer id : {result[0]}")
                        if result:
                            st.session_state.volunteer_id = result[0]
                finally:
                    timesheet.db_manager.release_connection(conn)
                
                # Restore session
                st.session_state["authentication_status"] = True
                st.session_state["username"] = username
                st.rerun()  # Immediately rerun to apply session state

            else:
                # If cookies fail, set explicit False to avoid limbo state
                st.session_state["authentication_status"] = False

        except Exception:
            st.session_state["authentication_status"] = False  # Ensure clean state

    # **Now decide what to render based on session state**
    if st.session_state.get("reset_password"):
        print("Password reset state on")
        timesheet.render_password_reset()  # Show password reset page
    elif st.session_state.get("authentication_status"):
        print("User authenticated")
        timesheet.render()  # Show main app
    else:
        print("Redirecting to login page")
        timesheet.render_authentication()  # Show login page

    # Clean up database connections when app is done
    # (This might not always execute in Streamlit's execution model)
    try:
        timesheet.db_manager.close_all_connections()
    except:
        pass

if __name__ == "__main__":
    main()