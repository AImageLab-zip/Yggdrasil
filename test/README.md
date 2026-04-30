# ToothFairy4M Upload Test

This directory contains scripts to test the upload API functionality.

## Files

- `test_upload.py` - Main upload test script with login and multi-modal upload
- `config.py` - Configuration file for credentials and file paths
- `run_test.py` - Simple runner script with user prompts
- `README.md` - This documentation

## Setup

1. **Update Configuration**
   
   Edit `config.py` to set your:
   - Login credentials (USERNAME, PASSWORD)
   - File paths for test files
   - Server URL if different

2. **Install Requirements**
   
   ```bash
   pip install requests
   ```

## Usage

### Option 1: Use the runner script (recommended)
```bash
python run_test.py
```

### Option 2: Run directly
```bash
python test_upload.py
```

### Option 3: From Docker container
```bash
# If running from inside the ToothFairy4M Docker container
docker-compose exec web-dev-llumetti python test/test_upload.py
```

## Configuration

### File Paths
Update the file paths in `config.py` to point to your test files:

```python
FILES_CONFIG = {
    "upper_scan": r"path\to\upper.stl",
    "lower_scan": r"path\to\lower.stl", 
    "cbct": r"path\to\cbct.nii.gz",
    "teleradiography": r"path\to\xray.jpg",
    "panoramic": r"path\to\panoramic.jpg",
    "intraoral_photos": [
        r"path\to\photo1.jpg",
        r"path\to\photo2.jpg",
        # ... up to 5 photos
    ],
    "rawzip": r"path\to\data.zip"  # Optional
}
```

### Credentials
Update your login credentials:

```python
USERNAME = "your_username"
PASSWORD = "your_password"
```

## What the script does

1. **Login** - Authenticates with the ToothFairy4M system
2. **Get Folders** - Retrieves available folders for organization
3. **Check Files** - Verifies all test files exist before upload
4. **Upload** - Sends a multi-modal patient with:
   - IOS files (upper.stl, lower.stl)
   - CBCT scan (.nii.gz file)
   - Teleradiography image
   - Panoramic image  
   - Multiple intraoral photos
   - RawZip file (if configured)
5. **Display Results** - Shows upload status and created jobs

## Expected Output

```
🦷 ToothFairy4M Upload Test
==================================================
🔑 Logging in...
✅ Login successful!

📁 Getting available folders...
✅ Found 3 folders:
   - ID: 1, Name: Folder A, Path: Folder A
   - ID: 2, Name: Folder B, Path: Folder B
   - ID: 3, Name: Test, Path: Test

📋 Checking files before upload:
   ✅ upper.stl (2,156,789 bytes)
   ✅ lower.stl (1,987,654 bytes)
   ✅ 1.nii.gz (45,123,456 bytes)
   ✅ licensed-image.jpg (2,345,678 bytes)
   ✅ licensed-image.jpg (2,345,678 bytes)
   Intraoral photos (5 files):
     ✅ licensed-image.jpg (2,345,678 bytes)
     ✅ licensed-image.jpg (2,345,678 bytes)
     ✅ licensed-image.jpg (2,345,678 bytes)
     ✅ licensed-image.jpg (2,345,678 bytes)
     ✅ licensed-image.jpg (2,345,678 bytes)

🚀 Starting upload with 10 files...

📤 Upload completed!
Status Code: 200
✅ Patient created successfully!
   Patient ID: 123
   Name: Multi Modal Patient Example
   Project: Maxillo Project
   Folder: Folder A
   Modalities (5):
     - Intra-Oral Scans (ios)
     - CBCT (cbct)
     - Teleradiography (teleradiography)
     - panoramic (panoramic)
     - Intraoral Photographs (intraoral)
   Processing Jobs (4):
     - Job #456: ios (pending)
     - Job #457: teleradiography (pending)
     - Job #458: panoramic (pending)
     - Job #459: intraoral (pending)

🎉 Upload completed successfully!
```

## Troubleshooting

### Common Issues

1. **Login Failed**
   - Check username/password in config.py
   - Verify server URL is correct

2. **File Not Found**
   - Check file paths in config.py
   - Ensure files exist and are readable

3. **Upload Failed**
   - Check server logs for detailed error messages
   - Verify project has the required modalities enabled
   - Check file formats are supported

### Debug Mode
Add this to the top of `test_upload.py` for more verbose output:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```
