from flask import Flask, render_template, request, redirect, url_for, session
from authlib.integrations.flask_client import OAuth
from google.cloud import storage, bigquery
import datetime
import uuid
import csv
import tempfile
import io
import os

app = Flask(__name__)

# Set the secret key for sessions (important for OAuth)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "f5b7a7f3e5d8c1b4b8f9e34d0a72f6f1")

# Initialize OAuth
oauth = OAuth(app)

# Google OAuth Configuration
google = oauth.register(
    'google',
    client_id=os.getenv('GOOGLE_CLIENT_ID'),
    client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),
    authorize_url='https://accounts.google.com/o/oauth2/auth',
    authorize_params=None,
    access_token_url='https://accounts.google.com/o/oauth2/token',
    refresh_token_url=None,
    client_kwargs={'scope': 'email', 'token_endpoint_auth_method': 'client_secret_post'},
)


# Replace with your bucket name and BigQuery details
BUCKET_NAME = 'etl_complexity_tool_src'
PROJECT_ID = 'transformers-200'
BQ_DATASET = 'etl_mig'
TABLE_NAMES = ['raw_etl_mappings_data_new', 'src_etl_map_tgt_tbl_new', 'src_tbl_sizes_new']  # Existing BigQuery tables
METADATA_TABLE = 'uploads_metadata'  # Metadata table for tracking uploads

@app.route('/')
def index():
    # Check if the user is logged in, if so, redirect to the upload page
    if 'google_token' in session:
        return redirect(url_for('upload_page'))  # Redirect to the upload page after login
    return render_template('login.html')  # Show login page if not authenticated

@app.route('/login')
def login():
    # Redirect user to Google's OAuth2 login page
    redirect_uri = url_for('authorized', _external=True)
    print(f"Redirect URI: {redirect_uri}")
    return google.authorize_redirect(redirect_uri)

@app.route('/logout')
def logout():
    # Clear the entire session
    session.clear()
    print("User has been logged out. Session cleared.")
    return redirect(url_for('index'))  # Redirect to the login page


@app.route('/oauth2callback')
def authorized():
    token = google.authorize_access_token()
    if token is None:
        return 'Access denied: reason={} error={}'.format(
            request.args['error_reason'],
            request.args['error_description']
        )
    
    session['google_token'] = token
    user_info = token.get('userinfo') or google.get('https://www.googleapis.com/oauth2/v1/userinfo').json()
    email = user_info.get('email')  # Extract the email
    session['email'] = email  # Save the email in the session

    print(f"User email stored in session: {email}")  # Debug log
    return redirect(url_for('upload_page'))


@app.route('/upload')
def upload_page():
    # Render the upload page only if the user is logged in
    if 'google_token' not in session:
        return redirect(url_for('index'))  # If not logged in, redirect to the login page
    return render_template('upload.html')

@app.route('/upload', methods=['POST'])
def upload():
    email = session.get('email')  # Get the email from session
    print(f"Email retrieved from session: {email}")  # Debug log

    if not email:
        return "Error: User email not found in session. Please log in again."
    # Ensure all 3 files are received
    files = [request.files.get(f'file{i+1}') for i in range(3)]
    if any(file is None or file.filename == '' for file in files):
        return 'Error: Please select all 3 files.'

    name = request.form.get('name')
    customer = request.form.get('customer')
    uploaded_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    guid = str(uuid.uuid4())  # Generate a unique ID for this upload batch
    email = session.get('email')  # Get the logged-in user's email from the session

    try:
        # Initialize clients
        storage_client = storage.Client()
        bq_client = bigquery.Client(project=PROJECT_ID)

        # Process each file and insert into corresponding BigQuery table
        for i, file in enumerate(files):
            table_name = f"{BQ_DATASET}.{TABLE_NAMES[i]}"
            file.seek(0)

            with tempfile.NamedTemporaryFile(mode='w+', newline='', delete=False) as tmpfile:
                reader = csv.DictReader(io.TextIOWrapper(file.stream, encoding='utf-8'))
                fieldnames = reader.fieldnames + ['guid']
                writer = csv.DictWriter(tmpfile, fieldnames=fieldnames)
                writer.writeheader()
                for row in reader:
                    row['guid'] = guid
                    writer.writerow(row)
                tmpfile_path = tmpfile.name

            job_config = bigquery.LoadJobConfig(
                source_format=bigquery.SourceFormat.CSV,
                skip_leading_rows=1,
                write_disposition="WRITE_APPEND",
                schema_update_options=["ALLOW_FIELD_ADDITION"],
            )

            with open(tmpfile_path, 'rb') as updated_file:
                load_job = bq_client.load_table_from_file(
                    updated_file, table_name, job_config=job_config
                )
            load_job.result()

        # Insert metadata into the metadata table
        metadata_table_ref = f"{PROJECT_ID}.{BQ_DATASET}.{METADATA_TABLE}"
        rows_to_insert = [{
            'name': name,
            'customer': customer,
            'guid': guid,
            'uploaded_at': uploaded_at,
            'email': email,  # Add email here
        }]
        errors = bq_client.insert_rows_json(metadata_table_ref, rows_to_insert)

        if errors:
            raise Exception(f"Error inserting metadata: {errors}")

        return f"Files uploaded successfully. GUID: {guid}"

    except Exception as e:
        return f"Error: {str(e)}"


@app.route('/check_form_entry', methods=['GET'])
def check_form_entry():
    if 'email' not in session:
        return {'entryExists': False, 'message': 'User not logged in'}, 401

    email = session['email']  # Get logged-in user's email
    bq_client = bigquery.Client(project=PROJECT_ID)

    # Query to check if the email exists in uploads_metadata
    query = f"""
        SELECT COUNT(*) as count
        FROM `{PROJECT_ID}.{BQ_DATASET}.{METADATA_TABLE}`
        WHERE email = @user_email
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("user_email", "STRING", email)
        ]
    )

    try:
        query_job = bq_client.query(query, job_config=job_config)
        result = list(query_job.result())
        entry_count = result[0].count if result else 0

        return {'entryExists': entry_count > 0, 'message': 'Check completed successfully'}
    except Exception as e:
        return {'entryExists': False, 'message': str(e)}, 500


# Instead of using `tokengetter`, you directly access the session token
def get_google_oauth_token():
    # Retrieve the stored access token for OAuth from the session
    return session.get('google_token')

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8080, debug=True)
