#!/bin/bash

set -

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
PROD_HOST=""
PROD_USER=""
DATASET_MOUNT_POINT="/dataset"
SSH_KEY_PATH="$HOME/.ssh/id_rsa"
PROD_DB_PORT="3306"
LOCAL_DB_PORT="3306"
SSH_TUNNEL_PID_FILE="/tmp/toothfairy_ssh_tunnel.pid"

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Function to check if a command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Function to check if mount point is already mounted
is_mounted() {
    mountpoint -q "$DATASET_MOUNT_POINT" 2>/dev/null
}

# Function to check if SSH tunnel is running
is_tunnel_running() {
    if [ -f "$SSH_TUNNEL_PID_FILE" ]; then
        local pid=$(cat "$SSH_TUNNEL_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0  # Tunnel is running
        else
            # PID file exists but process is dead
            rm -f "$SSH_TUNNEL_PID_FILE"
        fi
    fi
    return 1  # Tunnel is not running
}

# Function to start SSH tunnel
start_ssh_tunnel() {
    print_status "Checking SSH tunnel..."
    
    if is_tunnel_running; then
        print_success "SSH tunnel is already running"
        return 0
    fi
    
    print_status "Starting SSH tunnel to production database..."
    ssh -f -N -L "$LOCAL_DB_PORT:$PROD_HOST:$PROD_DB_PORT" "$PROD_USER@$PROD_HOST" -o ServerAliveInterval=15 -o ServerAliveCountMax=3
    
    if [ $? -eq 0 ]; then
        # Get the PID of the SSH process
        local tunnel_pid=$(pgrep -f "ssh.*-L.*$LOCAL_DB_PORT:$PROD_HOST:$PROD_DB_PORT")
        if [ -n "$tunnel_pid" ]; then
            echo "$tunnel_pid" > "$SSH_TUNNEL_PID_FILE"
            print_success "SSH tunnel started (PID: $tunnel_pid)"
            print_status "Production database accessible at localhost:$LOCAL_DB_PORT"
        else
            print_error "SSH tunnel started but could not find PID"
        fi
    else
        print_error "Failed to start SSH tunnel"
        exit 1
    fi
}

# Function to stop SSH tunnel
stop_ssh_tunnel() {
    if is_tunnel_running; then
        local pid=$(cat "$SSH_TUNNEL_PID_FILE")
        print_status "Stopping SSH tunnel (PID: $pid)..."
        kill "$pid" 2>/dev/null
        rm -f "$SSH_TUNNEL_PID_FILE"
        print_success "SSH tunnel stopped"
    else
        print_warning "SSH tunnel is not running"
    fi
}

# Function to mount dataset via sshfs
mount_dataset() {
    if mountpoint -q "$DATASET_MOUNT_POINT"; then
        print_success "Dataset is already mounted at $DATASET_MOUNT_POINT"
        return
    fi

    if [ -d "$DATASET_MOUNT_POINT" ]; then
        if [ "$(ls -A $DATASET_MOUNT_POINT)" ]; then
            print_error "$DATASET_MOUNT_POINT exists, is not a mount point, and is not empty. Aborting to avoid data loss."
            exit 1
        else
            print_status "Removing empty $DATASET_MOUNT_POINT and recreating..."
            sudo rmdir "$DATASET_MOUNT_POINT"
            sudo mkdir -p "$DATASET_MOUNT_POINT"
            sudo chown $USER:$USER "$DATASET_MOUNT_POINT"
            sudo chmod 700 "$DATASET_MOUNT_POINT"
        fi
    else
        print_status "Creating mount point $DATASET_MOUNT_POINT"
        sudo mkdir -p "$DATASET_MOUNT_POINT"
        sudo chown $USER:$USER "$DATASET_MOUNT_POINT"
        sudo chmod 700 "$DATASET_MOUNT_POINT"
    fi

    print_status "Mounting dataset from $PROD_HOST..."
    sshfs -o allow_other,default_permissions,reconnect,ServerAliveInterval=15,ServerAliveCountMax=3 \
        "$PROD_USER@$PROD_HOST:/dataset" "$DATASET_MOUNT_POINT"
    if [ $? -eq 0 ]; then
        print_success "Dataset mounted successfully"
    else
        print_error "Failed to mount dataset"
        exit 1
    fi
}

# Function to unmount dataset
unmount_dataset() {
    if is_mounted; then
        print_status "Unmounting dataset..."
        fusermount -u "$DATASET_MOUNT_POINT" 2>/dev/null || sudo umount "$DATASET_MOUNT_POINT"
        print_success "Dataset unmounted"
    else
        print_warning "Dataset is not mounted"
    fi
}

# Function to start development environment
start_dev() {
    print_status "Starting development environment..."
    
    # Check if .env.dev exists
    if [ ! -f ".env.dev" ]; then
        print_error ".env.dev file not found. Please create it first."
        exit 1
    fi
    
    # Check and mount dataset if not mounted
    if ! is_mounted; then
        print_status "Dataset not mounted, mounting now..."
        mount_dataset
    else
        print_success "Dataset is already mounted"
    fi
    
    # Check and start SSH tunnel if not running
    if ! is_tunnel_running; then
        print_status "SSH tunnel not running, starting now..."
        start_ssh_tunnel
    else
        print_success "SSH tunnel is already running"
    fi
    
    # Stop all containers first to ensure clean state
    print_status "Stopping all containers to ensure clean state..."
    docker-compose -f docker-compose.dev.yml --env-file .env.dev down 2>/dev/null || true
    

    
    # Start containers
    print_status "Starting Docker containers..."
    docker-compose -f docker-compose.dev.yml --env-file .env.dev up -d
    

    
    print_success "Development environment started!"
    print_status "Access your application at: http://localhost:8000"
    print_status "Database is available at: localhost:3306"
}

# Function to stop development environment
stop_dev() {
    print_status "Stopping development environment..."
    
    # Stop containers
    print_status "Stopping Docker containers..."
    docker-compose -f docker-compose.dev.yml --env-file .env.dev down
    
    # Stop SSH tunnel
    print_status "Removing SSH tunnel..."
    stop_ssh_tunnel
    
    # Unmount dataset
    print_status "Unmounting dataset..."
    unmount_dataset
    
    print_success "Development environment stopped!"
}



# Function to show logs
show_logs() {
    print_status "Showing container logs..."
    docker-compose -f docker-compose.dev.yml --env-file .env.dev logs -f
}

# Function to show status
show_status() {
    print_status "Development environment status:"
    echo
    
    # Check if containers are running
    if docker-compose -f docker-compose.dev.yml --env-file .env.dev ps | grep -q "Up"; then
        print_success "Containers are running"
        docker-compose -f docker-compose.dev.yml --env-file .env.dev ps
    else
        print_warning "Containers are not running"
    fi
    
    echo
    
    # Check SSH tunnel
    if is_tunnel_running; then
        local pid=$(cat "$SSH_TUNNEL_PID_FILE")
        print_success "SSH tunnel is running (PID: $pid)"
        
        # Check if MySQL is reachable through the tunnel
        if command -v nc >/dev/null 2>&1; then
            if nc -z localhost 3306 2>/dev/null; then
                print_success "MySQL is reachable at localhost:3306"
            else
                print_warning "MySQL is not reachable at localhost:3306"
            fi
        else
            print_status "MySQL reachability check skipped (netcat not available)"
        fi
    else
        print_warning "SSH tunnel is not running"
    fi
    
    echo
    
    # Check dataset mount
    if is_mounted; then
        print_success "Dataset is mounted at $DATASET_MOUNT_POINT"
    else
        print_warning "Dataset is not mounted"
    fi
}

# Function to show help
show_help() {
    echo "Yggdrasil Development Environment Script"
    echo
    echo "Usage: $0 [COMMAND]"
    echo
    echo "Commands:"
    echo "  start     Start development environment (mount dataset + SSH tunnel + containers)"
    echo "  stop      Stop development environment (stop containers + remove tunnel + unmount dataset)"
    echo "  status    Show current status"
    echo "  logs      Show container logs"
    echo
    echo "Examples:"
    echo "  $0 start      # Start everything"
    echo "  $0 status     # Check status"
    echo "  $0 logs       # View logs"
    echo "  $0 stop       # Stop everything"
}

# Main script logic
case "${1:-help}" in
    start)
        start_dev
        ;;
    stop)
        stop_dev
        ;;
    status)
        show_status
        ;;
    logs)
        show_logs
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        print_error "Unknown command: $1"
        echo
        show_help
        exit 1
        ;;
esac 