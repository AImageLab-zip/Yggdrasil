#!/usr/bin/env python3
"""
Test script for the new external API endpoints
Run this script to test the new API endpoints for external applications.

Usage: python test_external_api.py
"""

import requests
import json
import sys
from urllib.parse import urljoin

# Configuration
BASE_URL = "http://localhost:8000"  # Change this to your Django server URL
PROJECT_SLUG = "test-project"  # Change this to your project slug

def test_endpoint(url, method="GET", data=None, expected_status=200):
    """Test an API endpoint"""
    full_url = urljoin(BASE_URL, url)
    print(f"\n🧪 Testing {method} {url}")
    print(f"   Full URL: {full_url}")
    
    try:
        if method == "GET":
            response = requests.get(full_url)
        elif method == "POST":
            response = requests.post(full_url, json=data, headers={"Content-Type": "application/json"})
        
        print(f"   Status: {response.status_code}")
        
        if response.status_code == expected_status:
            print("   ✅ Status code matches expected")
        else:
            print(f"   ❌ Expected {expected_status}, got {response.status_code}")
        
        # Try to parse JSON response
        try:
            json_response = response.json()
            print(f"   Response type: {type(json_response)}")
            
            if isinstance(json_response, dict):
                print(f"   Success: {json_response.get('success', 'N/A')}")
                if 'error' in json_response:
                    print(f"   Error: {json_response['error']}")
                
                # Print some key info from successful responses
                if json_response.get('success'):
                    if 'total_patients' in json_response:
                        print(f"   Total patients: {json_response['total_patients']}")
                    if 'total_files' in json_response:
                        print(f"   Total files: {json_response['total_files']}")
                    if 'found_patients' in json_response:
                        print(f"   Found patients: {json_response['found_patients']}")
                
        except json.JSONDecodeError:
            print(f"   Response (first 200 chars): {response.text[:200]}")
        
        return response.status_code == expected_status
        
    except requests.RequestException as e:
        print(f"   ❌ Request failed: {e}")
        return False

def main():
    """Run all tests"""
    print("🚀 Testing Yggdrasil External API Endpoints")
    print(f"Base URL: {BASE_URL}")
    print(f"Project Slug: {PROJECT_SLUG}")
    
    # Test 1: Test upload endpoint (expect authentication error)
    test_endpoint(f"/api/{PROJECT_SLUG}/upload/", method="POST", data={}, expected_status=401)
    
    # Test 2: Get project patients and modalities (this might return 404 if no project with this slug)
    test_endpoint(f"/api/{PROJECT_SLUG}/patients/", expected_status=404)  # Expecting 404 if no project
    
    # Test 3: Get patient files (this might return 404 if no patient with ID 1)
    test_endpoint(f"/api/{PROJECT_SLUG}/patients/1/files/", expected_status=404)  # Expecting 404 if no patient
    
    # Test 4: Bulk patient files endpoint with empty list
    test_endpoint(f"/api/{PROJECT_SLUG}/patients/", method="POST", data={"patient_ids": []}, expected_status=400)
    
    # Test 5: Bulk patient files endpoint with non-existent patients
    test_endpoint(f"/api/{PROJECT_SLUG}/patients/", method="POST", data={"patient_ids": [999, 998, 997]}, expected_status=404)
    
    # Test 6: Bulk patient files endpoint with too many patients
    large_list = list(range(1, 102))  # 101 patient IDs
    test_endpoint(f"/api/{PROJECT_SLUG}/patients/", method="POST", data={"patient_ids": large_list}, expected_status=400)
    
    # Test 7: Test with a real project slug that might exist
    test_endpoint("/api/maxillo/patients/", expected_status=404)  # Try with 'maxillo' project slug
    
    print("\n📋 Test Summary:")
    print("- If you see 404 errors for projects/patients, that's expected if you don't have test data")
    print("- The important thing is that the endpoints respond and don't crash")
    print("- For full testing, create some test projects and patients in your Django admin")
    
    print("\n💡 Next steps to test with real data:")
    print("1. Start your Django server: python manage.py runserver")
    print("2. Create a project in Django admin (note the project slug)")
    print("3. Create patients with files in that project")
    print("4. Update PROJECT_SLUG in this script with your real project slug")
    print("5. Run the tests again")
    print(f"\n📝 Current test URLs:")
    print(f"   POST {BASE_URL}/api/{PROJECT_SLUG}/upload/")
    print(f"   GET  {BASE_URL}/api/{PROJECT_SLUG}/patients/")
    print(f"   GET  {BASE_URL}/api/{PROJECT_SLUG}/patients/<patient_id>/files/")
    print(f"   POST {BASE_URL}/api/{PROJECT_SLUG}/patients/")

if __name__ == "__main__":
    main()
