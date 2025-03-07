import secrets
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
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

class DatabaseManager:
    """Manages database connections and operations for the volunteer timesheet app."""
    
    def __init__(self):
        """Initialize the database connection pool."""
        # Database connection parameters
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
                        username VARCHAR(100) UNIQUE NOT NULL,
                        name VARCHAR(100) NOT NULL,
                        email VARCHAR(255) UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    
    def register_user(self, name, email, username, password):
        """Register a new user in the database."""
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
                
                # Insert the new user
                cursor.execute(
                    "INSERT INTO volunteers (username, name, email, password_hash) VALUES (%s, %s, %s, %s)",
                    (username, name, email, hashed_password)
                )
                conn.commit()
                
                # Update the credentials in memory
                self.load_credentials_from_db()
                
                return True, "Registration successful"
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

        with register_tab:
            try:
                if st.session_state.get('registration_success', False):
                    st.success('Registration successful! Please log in.')
                    st.session_state.registration_success = False

                reg_name = st.text_input('Name', key='reg_name')
                reg_email = st.text_input('Email', key='reg_email')
                reg_username = st.text_input('Username', key='reg_username')
                reg_password = st.text_input('Password', type='password', key='reg_password')
                reg_password_confirm = st.text_input('Confirm Password', type='password', key='reg_password_confirm')

                if st.button('Register'):
                    if reg_password != reg_password_confirm:
                        st.error('Passwords do not match')
                    else:
                        success, message = self.register_user(reg_name, reg_email, reg_username, reg_password)
                        if success:
                            st.session_state.registration_success = True
                            st.rerun()
                        else:
                            st.error(message)

            except Exception as e:
                st.error(f'An error occurred: {e}')


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
        tab1, tab2 = st.tabs(["🕒 Time Entry", "📊 Statistics"])

        with tab1:
            self.render_time_entry()

        with tab2:
            self.render_statistics()

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

    def render_statistics(self) -> None:
        """
        Renders volunteer statistics based on submitted timesheet data from the database.
        """
        st.header("Volunteer Hours Statistics")

        conn = self.db_manager.get_connection()
        try:
            # Total Volunteer Hours
            with conn.cursor() as cursor:
                cursor.execute("SELECT SUM(hours) FROM timesheets WHERE status = 'Approved'")
                result = cursor.fetchone()
                total_hours = result[0] if result[0] else 0
            
            # Most Active Project
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT p.name, SUM(t.hours) as total_hours
                    FROM timesheets t
                    JOIN projects p ON t.project_id = p.id
                    WHERE t.status = 'Approved'
                    GROUP BY p.name
                    ORDER BY total_hours DESC
                    LIMIT 1
                """)
                result = cursor.fetchone()
                most_active_project = result[0] if result else "N/A"
            
            # Estimate number of weeks
            with conn.cursor() as cursor:
                cursor.execute("SELECT MIN(date), MAX(date) FROM timesheets")
                result = cursor.fetchone()
                if result[0] and result[1]:
                    min_date, max_date = result[0], result[1]
                    num_weeks = max((max_date - min_date).days // 7, 1)
                else:
                    num_weeks = 1
            
            avg_weekly_hours = total_hours / num_weeks if num_weeks > 0 else 0
            
            # Number of unique projects
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(DISTINCT project_id) FROM timesheets")
                unique_projects = cursor.fetchone()[0]
            
            # Total unique volunteers
            with conn.cursor() as cursor:
                cursor.execute("SELECT COUNT(DISTINCT volunteer_id) FROM timesheets")
                total_volunteers = cursor.fetchone()[0]
            
            # Top 5 most active volunteers
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT v.name, SUM(t.hours) as total_hours
                    FROM timesheets t
                    JOIN volunteers v ON t.volunteer_id = v.id
                    WHERE t.status = 'Approved'
                    GROUP BY v.name
                    ORDER BY total_hours DESC
                    LIMIT 5
                """)
                top_volunteers_data = cursor.fetchall()
                top_volunteers = pd.DataFrame(top_volunteers_data)
            
            # Daily trend of volunteer hours
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT date, SUM(hours) as total_hours
                    FROM timesheets
                    GROUP BY date
                    ORDER BY date
                """)
                daily_hours_data = cursor.fetchall()
                daily_hours = pd.DataFrame(daily_hours_data)
            
            # Weekly trend of volunteer hours
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT DATE_TRUNC('week', date) AS week, SUM(hours) as total_hours
                    FROM timesheets
                    WHERE status = 'Approved'
                    GROUP BY week
                    ORDER BY week
                """)
                weekly_hours_data = cursor.fetchall()
                weekly_hours = pd.DataFrame(weekly_hours_data)
            
            # Project distribution
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT p.name, SUM(t.hours) as total_hours
                    FROM timesheets t
                    JOIN projects p ON t.project_id = p.id
                    WHERE t.status = 'Approved'
                    GROUP BY p.name
                    ORDER BY total_hours DESC
                """)
                project_data = cursor.fetchall()
                project_totals = pd.DataFrame(project_data)
            
            # Layout: Key statistics
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Total Volunteer Hours", f"{total_hours:.1f}")
                st.metric("Most Active Project", most_active_project)
            with col2:
                st.metric("Average Weekly Hours", f"{avg_weekly_hours:.1f}")
                st.metric("Projects Supported", f"{unique_projects}")
            with col3:
                st.metric("Total Volunteers", f"{total_volunteers}")
                if not top_volunteers.empty:
                    st.write("### 🏆 Top 5 Volunteers")
                    st.write(top_volunteers.rename(columns={"total_hours": "Total Hours"}))
            
            # Visualization: Hours per project (Pie Chart)
            st.subheader("📊 Project-wise Hour Distribution")
            if not project_totals.empty:
                fig, ax = plt.subplots()
                project_totals['total_hours'] = pd.to_numeric(project_totals['total_hours'], errors='coerce')
                project_totals.set_index('name')['total_hours'].plot(kind="pie", autopct="%1.1f%%", ax=ax)
                ax.set_ylabel("")
                st.pyplot(fig)
            else:
                st.info("No project data available for visualization.")
            
            # Visualization: Daily trend of hours logged
            st.subheader("📅 Daily Volunteer Hours Trend")
            if not daily_hours.empty:
                fig, ax = plt.subplots()
                daily_hours['total_hours'] = pd.to_numeric(daily_hours['total_hours'], errors='coerce')
                daily_hours.set_index('date')['total_hours'].plot(kind="bar", ax=ax, color="skyblue")
                ax.set_ylabel("Total Hours")
                ax.set_xlabel("Date")
                plt.xticks(rotation=45)
                st.pyplot(fig)
            else:
                st.info("No daily trend data available.")
            
            # Visualization: Weekly trend of hours logged
            st.subheader("📈 Weekly Volunteer Hours Trend")
            if not weekly_hours.empty:
                fig, ax = plt.subplots()
                weekly_hours['total_hours'] = pd.to_numeric(weekly_hours['total_hours'], errors='coerce')
                weekly_hours.set_index('week')['total_hours'].plot(kind="line", marker="o", linestyle="-", ax=ax, color="green")
                ax.set_ylabel("Total Hours")
                ax.set_xlabel("Week")
                plt.xticks(rotation=45)
                st.pyplot(fig)
            else:
                st.info("No weekly trend data available.")
                
        except Exception as e:
            st.error(f"Error retrieving statistics: {e}")
        finally:
            self.db_manager.release_connection(conn)


def main():
    st.set_page_config(page_title="MIMA Volunteer Timesheet", page_icon="🕒")

    # Initialize the timesheet application
    timesheet = VolunteerTimesheet()

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
    if st.session_state.get("authentication_status"):
        timesheet.render()  # Show main app
    else:
        timesheet.render_authentication()  # Show login page

    # Clean up database connections when app is done
    # (This might not always execute in Streamlit's execution model)
    try:
        timesheet.db_manager.close_all_connections()
    except:
        pass

if __name__ == "__main__":
    main()