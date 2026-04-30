# Configuration file for upload test

# Server Configuration
BASE_URL = "http://pdor.ing.unimore.it"
PROJECT_SLUG = "maxillo"

# Login Credentials
USERNAME = "admin"  # Replace with your username
PASSWORD = "your-password"  # Replace with your password

# File Paths Configuration
# Update these paths to match your local file system
FILES_CONFIG = {
    # IOS Files
    "upper_scan": r"E:\ToothFairy4M\Dataset_Progetto_AI\IOS\Progetto AI\Bits2Bites\1\upper.stl",
    "lower_scan": r"E:\ToothFairy4M\Dataset_Progetto_AI\IOS\Progetto AI\Bits2Bites\1\lower.stl",
    
    # CBCT File
    "cbct": r"E:\ToothFairy4M\Dataset_Progetto_AI\CBCT\niigz\1.nii.gz",
    
    # Image Files
    "teleradiography": r"C:\Users\Luca\Pictures\licensed-image.jpg",
    "panoramic": r"C:\Users\Luca\Pictures\licensed-image.jpg",
    
    # Multiple Intraoral Photos (list of file paths)
    "intraoral_photos": [
        r"C:\Users\Luca\Pictures\licensed-image.jpg",
        r"C:\Users\Luca\Pictures\licensed-image.jpg", 
        r"C:\Users\Luca\Pictures\licensed-image.jpg",
        r"C:\Users\Luca\Pictures\licensed-image.jpg",
        r"C:\Users\Luca\Pictures\licensed-image.jpg"
    ],
    
    # RawZip File (optional)
    "rawzip": r"C:\Users\Luca\Pictures\some_data.zip"  # Add if you have a zip file
}

# Upload Configuration
UPLOAD_CONFIG = {
    "patient_name": "Multi Modal Patient Example",
    "folder_id": "1",  # Default folder ID
    "modalities": "ios,cbct,teleradiography,panoramic,intraoral,rawzip"  # Include rawzip if needed
}

# Alternative file paths for Linux/Docker environment (if running from within container)
# Uncomment and modify these if running the script from inside the Docker container
LINUX_FILES_CONFIG = {
    "upper_scan": "/dataset/raw/ios/upper.stl",
    "lower_scan": "/dataset/raw/ios/lower.stl", 
    "cbct": "/dataset/raw/cbct/1.nii.gz",
    "teleradiography": "/dataset/raw/images/teleradiography.jpg",
    "panoramic": "/dataset/raw/images/panoramic.jpg",
    "intraoral_photos": [
        "/dataset/raw/images/intraoral1.jpg",
        "/dataset/raw/images/intraoral2.jpg",
        "/dataset/raw/images/intraoral3.jpg",
        "/dataset/raw/images/intraoral4.jpg",
        "/dataset/raw/images/intraoral5.jpg"
    ],
    "rawzip": "/dataset/raw/archives/data.zip"
}
