import requests
import json
import os
from pathlib import Path

# Import configuration
try:
    from config import BASE_URL, PROJECT_SLUG, USERNAME, PASSWORD, FILES_CONFIG, UPLOAD_CONFIG
    print("✅ Using config.py for settings")
except ImportError:
    print("⚠️  config.py not found, using default settings")
    # Default configuration
    BASE_URL = "http://pdor.ing.unimore.it:8080"
    PROJECT_SLUG = "maxillo"
    USERNAME = "llumetti"
    PASSWORD = "password"
    FILES_CONFIG = {
        "upper_scan": r"E:\ToothFairy4M\Dataset_Progetto_AI\IOS\Progetto AI\Bits2Bites\1\upper.stl",
        "lower_scan": r"E:\ToothFairy4M\Dataset_Progetto_AI\IOS\Progetto AI\Bits2Bites\1\lower.stl",
        "cbct": r"E:\ToothFairy4M\Dataset_Progetto_AI\CBCT\niigz\1.nii.gz",
        "teleradiography": r"C:\Users\Luca\Pictures\licensed-image.jpg",
        "panoramic": r"C:\Users\Luca\Pictures\licensed-image.jpg",
        "intraoral_photos": [r"C:\Users\Luca\Pictures\licensed-image.jpg"] * 5
    }
    UPLOAD_CONFIG = {
        "patient_name": "Multi Modal Patient Example",
        "folder_id": "1",
        "modalities": "ios,cbct,teleradiography,panoramic,intraoral"
    }

def login(username, password):
    """Login and get session cookies or token"""
    login_url = f"{BASE_URL}/accounts/login/"
    session = requests.Session()
    
    # Get CSRF token first
    try:
        response = session.get(login_url)
        if 'csrftoken' in session.cookies:
            csrf_token = session.cookies['csrftoken']
        else:
            # Try to extract from response content
            import re
            csrf_match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', response.text)
            if csrf_match:
                csrf_token = csrf_match.group(1)
            else:
                print("Could not find CSRF token")
                return None
    except Exception as e:
        print(f"Error getting login page: {e}")
        return None
    
    # Perform login
    login_data = {
        'username': username,
        'password': password,
        'csrfmiddlewaretoken': csrf_token
    }
    
    headers = {
        'Referer': login_url,
        'X-CSRFToken': csrf_token
    }
    
    try:
        response = session.post(login_url, data=login_data, headers=headers, allow_redirects=False)
        
        if response.status_code in [200, 302]:
            print("✅ Login successful!")
            return session
        else:
            print(f"❌ Login failed with status {response.status_code}")
            print(f"Response: {response.text[:500]}")
            return None
    except Exception as e:
        print(f"Error during login: {e}")
        return None

def get_folders(session):
    """Get available folders for the project"""
    folders_url = f"{BASE_URL}/api/{PROJECT_SLUG}/folders/"
    
    try:
        response = session.get(folders_url)
        if response.status_code == 200:
            data = response.json()
            print(f"✅ Found {data['total_folders']} folders:")
            for folder in data['folders']:
                print(f"   - ID: {folder['id']}, Name: {folder['name']}, Path: {folder['full_path']}")
            return data['folders']
        else:
            print(f"❌ Failed to get folders: {response.status_code}")
            return []
    except Exception as e:
        print(f"Error getting folders: {e}")
        return []

def check_file_exists(file_path):
    """Check if file exists and print info"""
    if os.path.exists(file_path):
        size = os.path.getsize(file_path)
        print(f"   ✅ {os.path.basename(file_path)} ({size:,} bytes)")
        return True
    else:
        print(f"   ❌ {file_path} - FILE NOT FOUND")
        return False

def upload_patient(session, folder_id="1"):
    """Upload patient with all modalities"""
    upload_url = f"{BASE_URL}/api/{PROJECT_SLUG}/upload/"
    
    print("\n📋 Checking files before upload:")
    files_to_open = []
    missing_files = []
    
    # Check single files
    for field_name, file_path in FILES_CONFIG.items():
        if field_name == "intraoral_photos":
            continue
        if isinstance(file_path, str) and check_file_exists(file_path):
            files_to_open.append((field_name, file_path))
        elif isinstance(file_path, str):
            missing_files.append(file_path)
    
    # Check intraoral photos
    print(f"   Intraoral photos ({len(FILES_CONFIG['intraoral_photos'])} files):")
    for i, file_path in enumerate(FILES_CONFIG['intraoral_photos']):
        if check_file_exists(f"     {file_path}"):
            files_to_open.append(("intraoral_folder_files", file_path))
        else:
            missing_files.append(file_path)
    
    if missing_files:
        print(f"\n❌ {len(missing_files)} files are missing. Cannot proceed with upload.")
        return False
    
    print(f"\n🚀 Starting upload with {len(files_to_open)} files...")
    
    # Prepare form data
    data = {
        "name": UPLOAD_CONFIG.get("patient_name", "Multi Modal Patient Example"),
        "folder": folder_id,
        # Note: modalities are now automatically inferred from uploaded files
    }
    
    # Open files
    files = []
    try:
        for field_name, file_path in files_to_open:
            files.append((field_name, open(file_path, "rb")))
        
        # Upload
        response = session.post(upload_url, data=data, files=files)
        
        print(f"\n📤 Upload completed!")
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            if result.get('success'):
                patient = result['patient']
                print(f"✅ Patient created successfully!")
                print(f"   Patient ID: {patient['patient_id']}")
                print(f"   Name: {patient['name']}")
                print(f"   Project: {patient['project']['name']}")
                
                if patient.get('folder'):
                    print(f"   Folder: {patient['folder']['full_path']}")
                
                print(f"   Modalities ({len(patient['modalities'])}):")
                for mod in patient['modalities']:
                    print(f"     - {mod['name']} ({mod['slug']})")
                
                if patient['upload_results']['jobs']:
                    print(f"   Processing Jobs ({len(patient['upload_results']['jobs'])}):")
                    for job in patient['upload_results']['jobs']:
                        print(f"     - Job #{job['id']}: {job['type']} ({job['status']})")
                
                return True
            else:
                print(f"❌ Upload failed: {result}")
                return False
        else:
            print(f"❌ Upload failed with status {response.status_code}")
            try:
                error_data = response.json()
                print(f"Error details: {json.dumps(error_data, indent=2)}")
            except:
                print(f"Error response: {response.text[:1000]}")
            return False
            
    except Exception as e:
        print(f"❌ Error during upload: {e}")
        return False
    finally:
        # Close all file handles
        for _, file_handle in files:
            try:
                file_handle.close()
            except:
                pass

def main():
    """Main function"""
    print("🦷 ToothFairy4M Upload Test")
    print("=" * 50)
    
    # Step 1: Login
    print("🔑 Logging in...")
    session = login(USERNAME, PASSWORD)
    if not session:
        print("❌ Login failed. Please check your credentials.")
        return
    
    # Step 2: Get folders (optional, for information)
    print("\n📁 Getting available folders...")
    folders = get_folders(session)
    
    # Step 3: Upload patient
    print("\n🦷 Uploading patient...")
    success = upload_patient(session, folder_id="1")  # Use folder ID 1, adjust as needed
    
    if success:
        print("\n🎉 Upload completed successfully!")
    else:
        print("\n💥 Upload failed!")

if __name__ == "__main__":
    main()