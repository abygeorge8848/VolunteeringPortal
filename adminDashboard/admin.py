import streamlit as st
import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor
import yaml
from yaml.loader import SafeLoader
import os
import secrets
from datetime import datetime, timedelta
import streamlit_authenticator as stauth
from configparser import ConfigParser
from dotenv import load_dotenv
import yagmail
import time
# Load environment variables from .env
load_dotenv()

class AdminDashboard:
    def __init__(self):
        # Initialize session state
        if "authentication_status" not in st.session_state:
            st.session_state["authentication_status"] = None

        if "username" not in st.session_state:
            st.session_state["username"] = None
            
        # Initialize DB connection
        self.conn = self.connect_to_database()
        
        # Load admin credentials initially from file for backward compatibility
        # Later we'll migrate this to fully use the database
        self.cookie_config = {
            'cookie_name': 'admin_app_cookie',
            'key': self.generate_secret_key(),
            'expiry_days': 30
        }
        
        # Admin credentials will now come from database
        self.admin_credentials = {'usernames': {}}
        self.load_admin_credentials_from_db()
        
        # Initialize authenticator
        self.authenticator = stauth.Authenticate(
            self.admin_credentials,
            self.cookie_config['cookie_name'],
            self.cookie_config['key'],
            self.cookie_config['expiry_days']
        )

    def connect_to_database(self):
        """Connect to PostgreSQL database using config."""
        try:
            # Try to get database connection parameters from environment variables first
            db_host = os.getenv("DB_HOST", "host.docker.internal")  # Use Docker's internal hostname
            db_port = os.getenv("DB_PORT")
            db_name = os.getenv("DB_NAME")
            db_user = os.getenv("DB_USER")
            db_password = os.getenv("DB_PASSWORD")
            
            # Alternatively, read from config file if it exists
            if os.path.exists('database.ini'):
                config = ConfigParser()
                config.read('database.ini')
                if 'postgresql' in config:
                    db_host = config['postgresql'].get('host', db_host)
                    db_port = config['postgresql'].get('port', db_port)
                    db_name = config['postgresql'].get('database', db_name)
                    db_user = config['postgresql'].get('user', db_user)
                    db_password = config['postgresql'].get('password', db_password)
            
            conn = psycopg2.connect(
                host=db_host,
                port=db_port,
                database=db_name,
                user=db_user,
                password=db_password
            )
            return conn
        except Exception as e:
            st.error(f"Database connection error: {e}")
            return None

    def generate_secret_key(self):
        """Generate a secret key for cookie encryption."""
        return secrets.token_hex(32)  # Generates a secure random key

    def load_admin_credentials_from_db(self):
        """Load admin credentials from the database."""
        if not self.conn:
            st.error("No database connection available.")
            return
        
        try:
            cursor = self.conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("SELECT id, name, email, password_hash FROM admins")
            admins = cursor.fetchall()
            cursor.close()
            
            # Clear existing credentials
            self.admin_credentials = {'usernames': {}}
            
            # If no admins exist, create a default admin
            if not admins:
                self.create_default_admin()
                return
            
            # Convert DB admins to the format expected by streamlit_authenticator
            for admin in admins:
                username = admin['email'].split('@')[0]  # Use part of email as username
                self.admin_credentials['usernames'][username] = {
                    'name': admin['name'],
                    'email': admin['email'],
                    'password': admin['password_hash']
                }
                
        except Exception as e:
            st.error(f"Error loading admin credentials: {e}")
            # Fallback to creating a default admin
            self.create_default_admin()

    def create_default_admin(self):
        """Create a default admin account in the database."""
        try:
            admin_name = "Admin"
            admin_email = "admin@example.com"
            admin_password = stauth.Hasher.hash('admin')  # Default password: admin
            
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT INTO admins (name, email, password_hash) VALUES (%s, %s, %s) ON CONFLICT (email) DO NOTHING",
                (admin_name, admin_email, admin_password)
            )
            self.conn.commit()
            cursor.close()
            
            # Add to local credentials
            self.admin_credentials['usernames']['admin'] = {
                'name': admin_name,
                'email': admin_email,
                'password': admin_password
            }
            
        except Exception as e:
            st.error(f"Error creating default admin: {e}")

    def register_new_admin(self, name, email, username, password):
        """Register a new admin in the database."""
        try:
            # Hash the password
            hashed_password = stauth.Hasher.hash(password)
            
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT INTO admins (name, email, password_hash) VALUES (%s, %s, %s) RETURNING id",
                (name, email, hashed_password)
            )
            admin_id = cursor.fetchone()[0]
            self.conn.commit()
            cursor.close()
            
            # Add to local credentials
            self.admin_credentials['usernames'][username] = {
                'name': name,
                'email': email,
                'password': hashed_password
            }
            
            return True, "Admin registered successfully."
        except psycopg2.errors.UniqueViolation:
            self.conn.rollback()
            return False, "Email already exists."
        except Exception as e:
            self.conn.rollback()
            return False, f"Error: {e}"

    def get_projects(self):
        """Get all projects from the database."""
        try:
            cursor = self.conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("SELECT * FROM projects ORDER BY name")
            projects = cursor.fetchall()
            cursor.close()
            return projects
        except Exception as e:
            st.error(f"Error fetching projects: {e}")
            return []

    def get_project_names(self):
        """Get list of project names."""
        projects = self.get_projects()
        return [project['name'] for project in projects]

    def add_project(self, project_name, admin_username):
        """Add a new project to the database."""
        try:
            # Get admin ID
            admin_id = self.get_admin_id_by_username(admin_username)
            if not admin_id:
                return False, "Admin not found."
            
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT INTO projects (name, created_by) VALUES (%s, %s) RETURNING id",
                (project_name, admin_id)
            )
            project_id = cursor.fetchone()[0]
            self.conn.commit()
            cursor.close()
            return True, f"Project '{project_name}' added successfully."
        except psycopg2.errors.UniqueViolation:
            self.conn.rollback()
            return False, "Project already exists."
        except Exception as e:
            self.conn.rollback()
            return False, f"Error: {e}"

    def get_admin_id_by_username(self, username):
        """Get admin ID by username (email prefix)."""
        admin_info = self.admin_credentials['usernames'].get(username, {})
        admin_email = admin_info.get('email')
        
        if not admin_email:
            return None
        
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT id FROM admins WHERE email = %s", (admin_email,))
            result = cursor.fetchone()
            cursor.close()
            return result[0] if result else None
        except Exception as e:
            st.error(f"Error getting admin ID: {e}")
            return None

    def delete_project(self, project_name):
        """Delete a project from the database."""
        try:
            # First check if project is being used in timesheets
            cursor = self.conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) FROM timesheets t
                JOIN projects p ON t.project_id = p.id
                WHERE p.name = %s
                """,
                (project_name,)
            )
            count = cursor.fetchone()[0]
            
            if count > 0:
                cursor.close()
                return False, "Cannot delete project that has hours logged against it."
            
            cursor.execute("DELETE FROM projects WHERE name = %s", (project_name,))
            self.conn.commit()
            cursor.close()
            return True, f"Project '{project_name}' deleted successfully."
        except Exception as e:
            self.conn.rollback()
            return False, f"Error: {e}"

    def get_pending_timesheets(self):
        """Get all pending timesheets from the database."""
        try:
            cursor = self.conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute(
                """
                SELECT t.id, v.name as volunteer_name, p.name as project, 
                       t.hours, t.date, t.status, v.id as volunteer_id
                FROM timesheets t
                JOIN volunteers v ON t.volunteer_id = v.id
                JOIN projects p ON t.project_id = p.id
                WHERE t.status = 'Pending'
                ORDER BY t.date DESC
                """
            )
            timesheets = cursor.fetchall()
            cursor.close()
            return timesheets
        except Exception as e:
            st.error(f"Error fetching pending timesheets: {e}")
            return []

    def approve_timesheet(self, timesheet_id):
        """Approve a timesheet entry."""
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE timesheets SET status = 'Approved' WHERE id = %s",
                (timesheet_id,)
            )
            self.conn.commit()
            cursor.close()
            return True, "Timesheet approved successfully."
        except Exception as e:
            self.conn.rollback()
            return False, f"Error: {e}"

    def get_approved_timesheets(self, volunteer_id=None, project_name=None, start_date=None, end_date=None):
        """Get approved timesheets with optional filters."""
        try:
            query = """
                SELECT t.id, v.name as volunteer_name, p.name as project, 
                       t.hours, t.date, t.status, v.id as volunteer_id
                FROM timesheets t
                JOIN volunteers v ON t.volunteer_id = v.id
                JOIN projects p ON t.project_id = p.id
                WHERE t.status = 'Approved'
            """
            params = []
            
            if volunteer_id:
                query += " AND v.id = %s"
                params.append(volunteer_id)
                
            if project_name:
                query += " AND p.name = %s"
                params.append(project_name)
                
            if start_date:
                query += " AND t.date >= %s"
                params.append(start_date)
                
            if end_date:
                query += " AND t.date <= %s"
                params.append(end_date)
                
            query += " ORDER BY t.date DESC"
            # print(f"The query is : {query}")
            cursor = self.conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute(query, params)
            timesheets = cursor.fetchall()
            cursor.close()
            return timesheets
        except Exception as e:
            st.error(f"Error fetching approved timesheets: {e}")
            return []

    def get_volunteers(self):
        """Get all volunteers from the database."""
        try:
            cursor = self.conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("SELECT id, name, email FROM volunteers ORDER BY name")
            volunteers = cursor.fetchall()
            cursor.close()
            return volunteers
        except Exception as e:
            st.error(f"Error fetching volunteers: {e}")
            return []

    def get_volunteer_stats(self):
        """Get volunteer statistics (total hours, etc.)."""
        try:
            cursor = self.conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute(
                """
                SELECT v.id, v.name, v.email, 
                       COALESCE(SUM(t.hours), 0) as total_hours,
                       COUNT(DISTINCT p.id) as projects_count
                FROM volunteers v
                LEFT JOIN timesheets t ON v.id = t.volunteer_id AND t.status = 'Approved'
                LEFT JOIN projects p ON t.project_id = p.id
                GROUP BY v.id, v.name, v.email
                ORDER BY total_hours DESC
                """
            )
            stats = cursor.fetchall()
            cursor.close()
            return stats
        except Exception as e:
            st.error(f"Error fetching volunteer statistics: {e}")
            return []

    def export_timesheet_data(self, data):
        """Convert timesheet data to DataFrame and export to CSV."""
        try:
            if not data:
                return False, "No data to export."
                
            df = pd.DataFrame(data)
            df.to_csv("exported_hours.csv", index=False)
            return True, "Data exported to exported_hours.csv"
        except Exception as e:
            return False, f"Error exporting data: {e}"

    def render_authentication(self):
        """Render authentication page with login and registration options for admins."""
        st.title("MIMA Volunteer Management - Admin Dashboard")

        # Create tabs for login and registration
        login_tab, register_tab = st.tabs(["Login", "Register New Admin"])

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
                self.render_dashboard()
            else:
                if st.session_state.get('FormSubmitter:Login-Login', False):
                    st.error('Incorrect username or password. Please try again.')
                else:
                    st.warning('Please enter your admin username and password.')
            
            if st.button("Forgot Password?"):
                st.session_state["reset_password"] = True
                st.rerun()

        with register_tab:
            try:
                if st.session_state.get('registration_success', False):
                    st.success('Admin registration successful! Please log in.')

                reg_name = st.text_input('Name', key='reg_name')
                reg_email = st.text_input('Email', key='reg_email')
                reg_username = st.text_input('Username', key='reg_username')
                reg_password = st.text_input('Password', type='password', key='reg_password')
                reg_password_confirm = st.text_input('Confirm Password', type='password', key='reg_password_confirm')

                if st.button('Register Admin'):
                    if reg_password != reg_password_confirm:
                        st.error('Passwords do not match')
                    elif reg_username in self.admin_credentials['usernames']:
                        st.error('Username already exists')
                    else:
                        success, message = self.register_new_admin(
                            reg_name,
                            reg_email,
                            reg_username,
                            reg_password
                        )
                        
                        if success:
                            # Set registration success flag
                            st.session_state.registration_success = True
                            st.success(message)
                            # Rerun to refresh the page
                            st.rerun()
                        else:
                            st.error(message)

            except Exception as e:
                st.error(f'An error occurred: {e}')

    def render_dashboard(self):
        """Render the admin dashboard."""
        # Header with title and logout button
        col1, col2 = st.columns([3, 1])
        with col1:
            st.title("Admin Dashboard")
        with col2:
            # Align the logout button to the right side of the header
            if st.button("Logout", key="logout_btn"):
                st.session_state.authentication_status = None
                st.session_state.username = None
                st.rerun()
        
        # Create tabs for different admin functions
        pending_tab, approved_tab, volunteers_tab, projects_tab = st.tabs([
            "Pending Hours", "Approved Hours", "Volunteers", "Manage Projects"
        ])
        
        with pending_tab:
            self.render_pending_hours()
            
        with approved_tab:
            self.render_approved_hours()
            
        with volunteers_tab:
            self.render_volunteers_list()
            
        with projects_tab:
            self.render_project_management()
    
    def render_pending_hours(self):
        """Render the pending hours tab."""
        st.header("Pending Hours for Approval")
        
        # Get pending hours from DB
        pending_timesheets = self.get_pending_timesheets()
        
        if not pending_timesheets:
            st.info("No pending hours to approve.")
        else:
            # Convert to DataFrame for display
            pending_df = pd.DataFrame(pending_timesheets)
            selected_columns = ['volunteer_name', 'project', 'hours', 'date']
            if all(col in pending_df.columns for col in selected_columns):
                st.dataframe(pending_df[selected_columns], use_container_width=True)
            
            # Create a selection mechanism
            for i, timesheet in enumerate(pending_timesheets):
                col1, col2 = st.columns([4, 1])
                with col1:
                    st.write(f"**{timesheet['volunteer_name']}** worked {timesheet['hours']} hours on **{timesheet['project']}** on {timesheet['date']}")
                with col2:
                    if st.button(f"Approve", key=f"approve_{i}"):
                        success, message = self.approve_timesheet(timesheet['id'])
                        if success:
                            st.success(message)
                            st.rerun()
                        else:
                            st.error(message)
    
    def render_approved_hours(self):
        """Render the approved hours tab."""
        st.header("Approved Volunteer Hours")
        
        # Add filters
        cols = st.columns(3)
        
        # Get volunteers for filter
        volunteers = self.get_volunteers()
        volunteer_dict = {vol['id']: vol['name'] for vol in volunteers}
        volunteer_options = ['All'] + [vol['name'] for vol in volunteers]
        
        with cols[0]:
            selected_volunteer = st.selectbox("Filter by Volunteer", volunteer_options)
            # Convert name to ID for query
            selected_volunteer_id = None
            if selected_volunteer != 'All':
                for vol_id, vol_name in volunteer_dict.items():
                    if vol_name == selected_volunteer:
                        selected_volunteer_id = vol_id
                        break
        
        # Get projects for filter
        projects = self.get_project_names()
        with cols[1]:
            selected_project = st.selectbox("Filter by Project", ['All'] + projects)
            # For query
            if selected_project == 'All':
                selected_project = None
        
        with cols[2]:
            date_range = st.date_input("Filter by Date Range", 
                                    [datetime.now() - timedelta(days=30), datetime.now()],
                                    format="YYYY-MM-DD")
        
        # Get filtered data
        start_date = date_range[0] if len(date_range) > 0 else None
        end_date = date_range[1] if len(date_range) > 1 else None
        
        approved_timesheets = self.get_approved_timesheets(
            volunteer_id=selected_volunteer_id,
            project_name=selected_project,
            start_date=start_date,
            end_date=end_date
        )
        
        if not approved_timesheets:
            st.info("No approved hours match your filter criteria.")
        else:
            # Convert to DataFrame for display
            approved_df = pd.DataFrame(approved_timesheets)
            selected_columns = ['volunteer_name', 'project', 'hours', 'date']
            if all(col in approved_df.columns for col in selected_columns):
                st.dataframe(approved_df[selected_columns], use_container_width=True)
                
                # Summary statistics
                total_hours = sum(sheet['hours'] for sheet in approved_timesheets)
                st.metric("Total Hours", f"{total_hours:.1f}")
                
                # Export option
                if st.button("Export to CSV"):
                    success, message = self.export_timesheet_data(approved_timesheets)
                    if success:
                        st.success(message)
                    else:
                        st.error(message)
    
    def render_volunteers_list(self):
        """Render the volunteers list tab."""
        st.header("Registered Volunteers")
        
        # Get volunteers with stats
        volunteer_stats = self.get_volunteer_stats()
        
        if not volunteer_stats:
            st.info("No volunteers registered yet.")
        else:
            # Convert to DataFrame for display
            volunteers_df = pd.DataFrame(volunteer_stats)
            basic_columns = ['name', 'email']
            if all(col in volunteers_df.columns for col in basic_columns):
                st.dataframe(volunteers_df[basic_columns], use_container_width=True)
            
            # Display volunteer statistics
            st.subheader("Volunteer Statistics")
            stat_columns = ['name', 'total_hours', 'projects_count']
            if all(col in volunteers_df.columns for col in stat_columns):
                stats_df = volunteers_df[stat_columns].copy()
                # Format total_hours to 1 decimal place
                stats_df['total_hours'] = stats_df['total_hours'].apply(lambda x: f"{x:.1f}")
                st.dataframe(stats_df, use_container_width=True)
    
    def render_project_management(self):
        """Render the project management tab."""
        st.header("Manage Projects")
        
        # Display current projects
        st.subheader("Current Projects")
        projects = self.get_projects()
        
        if not projects:
            st.info("No projects available.")
        else:
            projects_df = pd.DataFrame(projects)
            display_columns = ['name']
            if 'created_at' in projects_df.columns:
                display_columns.append('created_at')
            st.dataframe(projects_df[display_columns], use_container_width=True)
        
        # Add new project
        st.subheader("Add New Project")
        new_project = st.text_input("Project Name")
        if st.button("Add Project"):
            if new_project:
                success, message = self.add_project(new_project, st.session_state.username)
                if success:
                    st.success(message)
                    st.rerun()
                else:
                    st.error(message)
            else:
                st.error("Please enter a project name.")
        
        # Delete existing project
        project_names = self.get_project_names()
        if project_names:
            st.subheader("Delete Project")
            project_to_delete = st.selectbox("Select Project to Delete", project_names)
            if st.button("Delete Project"):
                success, message = self.delete_project(project_to_delete)
                if success:
                    st.success(message)
                    st.rerun()
                else:
                    st.error(message)

    
    def reset_password(self, token, new_password):
        """Reset password using a token."""
        # conn = self.db_manager.get_connection()
        
        try:
            with self.conn.cursor() as cursor:
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
                    UPDATE admins 
                    SET password_hash = %s 
                    WHERE email = %s
                """, (hashed_password, email))

                # Delete the used token
                cursor.execute("DELETE FROM password_reset_tokens WHERE email = %s", (email,))

                self.conn.commit()

                # ✅ Clear session state for reset process
                st.session_state.pop("reset_password", None)
                st.session_state.pop("reset_token", None)

                return True, "Password successfully reset."
        finally:
            print("Release the conection")
            cursor.close()
            # self.db_manager.release_connection(conn)


    def create_reset_token(self, email):
        """Generate and store a password reset token for the given email."""
        # conn = self.get_connection()
        try:
            with self.conn.cursor() as cursor:
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

                self.conn.commit()
                return token  # Return the token for sending in the email
        except Exception as e:
            self.conn.rollback()
            st.error(f"Error creating reset token: {e}")
        finally:
            cursor.close()


    def send_reset_email(self, email):
        """Send password reset email with a unique token link."""
        # conn = self.db_manager.get_connection()
        try:
            with self.conn.cursor() as cursor:
                cursor.execute("SELECT name FROM admins WHERE email = %s", (email,))
                user = cursor.fetchone()
                print(f"The user is : {user}")

                if not user:
                    return False, "Email not found in our records."

            # Generate and store a token
            token = self.create_reset_token(email)
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
            cursor.close()
            print("Release the connection")
            # self.db_manager.release_connection(conn)


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


    def run(self):
        """Main entry point for the admin dashboard."""
        if not self.conn:
            st.error("Could not connect to the database. Please check your configuration.")
            # Display database configuration helper
            with st.expander("Database Configuration Help"):
                st.write("""
                To configure your database connection, create a file named `database.ini` with the following content:
                
                ```
                [postgresql]
                host=localhost
                port=5432
                database=volunteer_app
                user=postgres
                password=your_password
                ```
                
                Or set the following environment variables:
                - DB_HOST
                - DB_PORT
                - DB_NAME
                - DB_USER
                - DB_PASSWORD
                """)
            return
        
        query_params = st.query_params
        if "reset_token" in query_params and "reset_password" not in st.session_state:
            st.session_state["reset_token"] = query_params["reset_token"]
            st.session_state["reset_password"] = True
            st.rerun()  # Set state and rerun to render the correct page
    
        if st.session_state.get("reroute_to_login"):
            st.session_state.pop("reroute_to_login")
            st.rerun()
            
        if st.session_state.get("reset_password"):
            self.render_password_reset()  # Show password reset page
        elif st.session_state.get('authentication_status'):
            self.render_dashboard()
        else:
            self.render_authentication()

# Entry point for the Streamlit app
if __name__ == "__main__":
    admin_dashboard = AdminDashboard()
    admin_dashboard.run()