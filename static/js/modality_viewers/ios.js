/**
 * IOS (Intraoral Scan) Viewer
 * Handles 3D visualization of upper and lower jaw STL files
 */

// STL Loader - simplified version
THREE.STLLoader = function() {
    this.manager = THREE.DefaultLoadingManager;
};

THREE.STLLoader.prototype = {
    constructor: THREE.STLLoader,
    
    load: function(url, onLoad, onProgress, onError) {
        var scope = this;
        var loader = new THREE.FileLoader(scope.manager);
        loader.setResponseType('arraybuffer');
        loader.load(url, function(data) {
            try {
                onLoad(scope.parse(data));
            } catch (e) {
                if (onError) {
                    onError(e);
                } else {
                    console.error(e);
                }
                scope.manager.itemError(url);
            }
        }, onProgress, onError);
    },
    
    parse: function(data) {
        var geometry = new THREE.BufferGeometry();
        
        // Simple ASCII STL parser
        var dataString = new TextDecoder().decode(data);
        
        if (dataString.indexOf('solid') === 0) {
            // ASCII format
            var vertices = [];
            var normals = [];
            
            var lines = dataString.split('\n');
            for (var i = 0; i < lines.length; i++) {
                var line = lines[i].trim();
                if (line.startsWith('vertex')) {
                    var coords = line.split(/\s+/);
                    vertices.push(parseFloat(coords[1]), parseFloat(coords[2]), parseFloat(coords[3]));
                } else if (line.startsWith('facet normal')) {
                    var coords = line.split(/\s+/);
                    var nx = parseFloat(coords[2]);
                    var ny = parseFloat(coords[3]);
                    var nz = parseFloat(coords[4]);
                    // Add normal for each of the 3 vertices of the triangle
                    normals.push(nx, ny, nz, nx, ny, nz, nx, ny, nz);
                }
            }
            
            geometry.setAttribute('position', new THREE.Float32BufferAttribute(vertices, 3));
            geometry.setAttribute('normal', new THREE.Float32BufferAttribute(normals, 3));
        } else {
            // Binary format - simplified parser
            var view = new DataView(data);
            var triangles = view.getUint32(80, true);
            
            var vertices = [];
            var normals = [];
            
            for (var i = 0; i < triangles; i++) {
                var offset = 84 + i * 50;
                
                // Normal
                var nx = view.getFloat32(offset, true);
                var ny = view.getFloat32(offset + 4, true);
                var nz = view.getFloat32(offset + 8, true);
                
                // Vertices
                for (var j = 0; j < 3; j++) {
                    var vx = view.getFloat32(offset + 12 + j * 12, true);
                    var vy = view.getFloat32(offset + 16 + j * 12, true);
                    var vz = view.getFloat32(offset + 20 + j * 12, true);
                    
                    vertices.push(vx, vy, vz);
                    normals.push(nx, ny, nz);
                }
            }
            
            geometry.setAttribute('position', new THREE.Float32BufferAttribute(vertices, 3));
            geometry.setAttribute('normal', new THREE.Float32BufferAttribute(normals, 3));
        }
        
        return geometry;
    }
};

// =====================================================
// 3D VIEWER IMPLEMENTATION
// =====================================================

// Scene background follows the sitewide data-theme stamp on <html> (set pre-paint
// in base.html, toggled by static/js/nav.js). Light matches --ygg-surface-sunken
// (#eef1f5-ish, kept as the historical 0xf0f0f0); dark matches the dark value of
// the same token (#152036) so the WebGL canvas blends into #scan-viewer. The
// "white background" display toggle overrides this with a pure white scene.
function iosThemeBackgroundColor() {
    return document.documentElement.getAttribute('data-theme') === 'dark'
        ? new THREE.Color(0x152036)
        : new THREE.Color(0xf0f0f0);
}

function iosSceneBackgroundColor() {
    return landmarkState1.whiteBackground ? new THREE.Color(0xffffff) : iosThemeBackgroundColor();
}

// Global variables for 3D scene
let scene1, camera1, renderer1;
let controls1;
let upperMesh1, lowerMesh1;
let cameraLight1;
let gridOverlay1;
let referenceAxis1;
let landmarkMarkers1;
const landmarkState1 = {
    active: false,
    // View-only visibility of saved landmarks (eye button), independent of the
    // annotation workbench. Markers render when showLandmarks OR active.
    showLandmarks: false,
    // Per-type visibility map (all types visible by default). Keys are filled
    // lazily from landmarkTypes1; undefined reads as visible.
    visibleTypes: {},
    selectedTooth: '',
    selectedType: null,
    tool: 'place',
    selectedMarker: null,
    landmarks: {},
    dirty: false,
    undoStack: [],
    markerSize: 0.65,
    showAxis: true,
    whiteBackground: false
};

function isLandmarkTypeVisible(type) {
    return landmarkState1.visibleTypes[type] !== false;
}
const landmarkTypes1 = [
    'incisal', 'outer', 'bracket', 'gingival', 'mesial', 'distal', 'inner', 'facial', 'cusps', 'planar'
];
const editableLandmarkTypes1 = landmarkTypes1.filter(type => type !== 'planar');
const landmarkTypeLabels1 = {
    incisal: 'Incisal', outer: 'Outer', bracket: 'Bracket', gingival: 'Gingival',
    mesial: 'Mesial', distal: 'Distal', inner: 'Inner', facial: 'Facial', cusps: 'Cusps'
};
const landmarkColors1 = {
    incisal: 0xf97316, outer: 0x2563eb, bracket: 0x7c3aed, gingival: 0xdc2626,
    mesial: 0x16a34a, distal: 0x0891b2, inner: 0x4f46e5, facial: 0xdb2777,
    cusps: 0xca8a04, planar: 0x64748b
};
const landmarkTeeth1 = [
    '18', '17', '16', '15', '14', '13', '12', '11',
    '21', '22', '23', '24', '25', '26', '27', '28',
    '48', '47', '46', '45', '44', '43', '42', '41',
    '31', '32', '33', '34', '35', '36', '37', '38'
];

// Initialize 3D viewer
function initViewer(containerId, upperStlUrl, lowerStlUrl, retryCount = 0) {
    const container = document.getElementById(containerId);
    console.debug('IOS initViewer called with containerId:', containerId, 'retry:', retryCount);
    console.debug('Container element:', container);
    
    if (!container) {
        console.error('IOS viewer container not found:', containerId);
        return;
    }
    
    // Make sure container is visible
    if (container.style.display === 'none') {
        console.debug('Removing display:none from container');
        container.style.display = '';
    }
    
    console.debug('Container dimensions:', container.clientWidth, 'x', container.clientHeight);
    
    if (container.clientWidth === 0 || container.clientHeight === 0) {
        if (retryCount < 20) { // Max 2 seconds of retries
            console.warn('Container has zero dimensions, retrying in 100ms... (attempt', retryCount + 1, 'of 20)');
            setTimeout(() => initViewer(containerId, upperStlUrl, lowerStlUrl, retryCount + 1), 100);
        } else {
            console.error('Failed to initialize IOS viewer: container has no dimensions after 2 seconds');
            window.IOSViewer.loading = false; // Reset loading state
        }
        return;
    }
    
    console.debug('Container ready, initializing 3D viewer...');
    const loadingIndicator = null; // No loading indicator element for now
    
    // Create scene
    const scene = new THREE.Scene();
    scene.background = iosSceneBackgroundColor();
    
    // Create camera
    const camera = new THREE.PerspectiveCamera(35, container.clientWidth / container.clientHeight, 0.1, 1000);
    camera.position.set(0, 80, 0);
    camera.up.set(0, 0, -1); // Set Z-axis as up
    camera.lookAt(0, 0, 0);
    
    // Create renderer
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(container.clientWidth, container.clientHeight);
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.domElement.style.position = 'absolute';
    renderer.domElement.style.top = '0';
    renderer.domElement.style.left = '0';
    container.appendChild(renderer.domElement);
    
    // Add lighting
    const cameraLight = new THREE.DirectionalLight(0xffffff, 0.9);
    cameraLight.position.copy(camera.position);
    cameraLight.target.position.set(0, 0, 0);
    scene.add(cameraLight);
    scene.add(cameraLight.target);
    
    // Add controls
    const controls = new THREE.TrackballControls(camera, renderer.domElement);
    controls.rotateSpeed = 2.0;
    controls.zoomSpeed = 1.2;
    controls.panSpeed = 0.8;
    controls.noZoom = false;
    controls.noPan = false;
    controls.noRotate = false;
    controls.staticMoving = true;
    controls.dynamicDampingFactor = 0.3;
    controls.target.set(0, 0, 0);
    controls.screen.left = 0;
    controls.screen.top = 0;
    controls.screen.width = container.clientWidth;
    controls.screen.height = container.clientHeight;
    controls.handleResize();
    
    // Add reference frame axis
    referenceAxis1 = addReferenceAxis(scene);
    referenceAxis1.visible = landmarkState1.showAxis;

    // Store references
    scene1 = scene;
    camera1 = camera;
    renderer1 = renderer;
    controls1 = controls;
    cameraLight1 = cameraLight;
    landmarkMarkers1 = new THREE.Group();
    scene.add(landmarkMarkers1);

    // Keep the scene background in sync when the user toggles the theme.
    if (!window.__iosThemeObserver) {
        window.__iosThemeObserver = new MutationObserver(function () {
            if (scene1) scene1.background = iosSceneBackgroundColor();
        });
        window.__iosThemeObserver.observe(document.documentElement, {
            attributes: true,
            attributeFilter: ['data-theme']
        });
    }
    
    // Create grid helper (hidden by default)
    createGrid(9); // Default to 9x9 grid

    renderer.domElement.addEventListener('pointerdown', onLandmarkPointerDown, true);
    
    // Load STL files
    loadSTLFiles(scene, loadingIndicator, upperStlUrl, lowerStlUrl);
    
    // Start animation loop
    animate();
}

// Add reference frame axis
function addReferenceAxis(scene) {
    const axisLength = 10;
    const axisWidth = 0.2;
    const axisGroup = new THREE.Group();
    
    // X-axis (Red) - pointing left
    const xGeometry = new THREE.CylinderGeometry(axisWidth, axisWidth, axisLength, 8);
    const xMaterial = new THREE.MeshBasicMaterial({ color: 0xff0000 });
    const xAxis = new THREE.Mesh(xGeometry, xMaterial);
    xAxis.rotation.z = Math.PI / 2;
    xAxis.position.x = -axisLength / 2;
    axisGroup.add(xAxis);
    
    const xArrowGeometry = new THREE.ConeGeometry(axisWidth * 2, axisWidth * 4, 8);
    const xArrow = new THREE.Mesh(xArrowGeometry, xMaterial);
    xArrow.rotation.z = Math.PI / 2;
    xArrow.position.x = -axisLength - axisWidth * 2;
    axisGroup.add(xArrow);
    
    // Y-axis (Green) - pointing up
    const yGeometry = new THREE.CylinderGeometry(axisWidth, axisWidth, axisLength, 8);
    const yMaterial = new THREE.MeshBasicMaterial({ color: 0x00ff00 });
    const yAxis = new THREE.Mesh(yGeometry, yMaterial);
    yAxis.position.y = axisLength / 2;
    axisGroup.add(yAxis);
    
    const yArrowGeometry = new THREE.ConeGeometry(axisWidth * 2, axisWidth * 4, 8);
    const yArrow = new THREE.Mesh(yArrowGeometry, yMaterial);
    yArrow.position.y = axisLength + axisWidth * 2;
    axisGroup.add(yArrow);
    
    // Z-axis (Blue) - pointing backward
    const zGeometry = new THREE.CylinderGeometry(axisWidth, axisWidth, axisLength, 8);
    const zMaterial = new THREE.MeshBasicMaterial({ color: 0x0000ff });
    const zAxis = new THREE.Mesh(zGeometry, zMaterial);
    zAxis.rotation.x = -Math.PI / 2;
    zAxis.position.z = -axisLength / 2;
    axisGroup.add(zAxis);
    
    const zArrowGeometry = new THREE.ConeGeometry(axisWidth * 2, axisWidth * 4, 8);
    const zArrow = new THREE.Mesh(zArrowGeometry, zMaterial);
    zArrow.rotation.x = -Math.PI / 2;
    zArrow.position.z = -axisLength - axisWidth * 2;
    axisGroup.add(zArrow);
    
    // Add text labels
    addAxisLabels(axisGroup, axisLength);
    
    scene.add(axisGroup);
    return axisGroup;
}

// Add text labels for the axes
function addAxisLabels(axisGroup, axisLength) {
    const labelOffset = axisLength + 3;
    
    function createTextTexture(text, color) {
        const canvas = document.createElement('canvas');
        const context = canvas.getContext('2d');
        canvas.width = 64;
        canvas.height = 64;
        
        context.fillStyle = color;
        context.font = 'bold 48px Arial';
        context.textAlign = 'center';
        context.textBaseline = 'middle';
        context.fillText(text, 32, 32);
        
        return new THREE.CanvasTexture(canvas);
    }
    
    // X-axis label (0)
    const xLabel = new THREE.Sprite(new THREE.SpriteMaterial({ map: createTextTexture('0', '#ff0000') }));
    xLabel.position.set(-labelOffset, 0, 0);
    xLabel.scale.set(2, 2, 1);
    axisGroup.add(xLabel);
    
    // Y-axis label (1)
    const yLabel = new THREE.Sprite(new THREE.SpriteMaterial({ map: createTextTexture('1', '#00ff00') }));
    yLabel.position.set(0, labelOffset, 0);
    yLabel.scale.set(2, 2, 1);
    axisGroup.add(yLabel);
    
    // Z-axis label (2)
    const zLabel = new THREE.Sprite(new THREE.SpriteMaterial({ map: createTextTexture('2', '#0000ff') }));
    zLabel.position.set(0, 0, -labelOffset);
    zLabel.scale.set(2, 2, 1);
    axisGroup.add(zLabel);
}

// Load STL files
function loadSTLFiles(scene, loadingIndicator, upperStlUrl, lowerStlUrl) {
    const loader = new THREE.STLLoader();
    let meshesLoaded = 0;
    
    function onModelLoaded() {
        meshesLoaded++;
        if (meshesLoaded === 2) {
            if (loadingIndicator) {
                loadingIndicator.style.display = 'none';
            }
            
            console.debug('Both meshes loaded successfully');
            
            centerScansAtOrigin();
            
            console.debug('Scans centered and camera positioned');
            
            // Ensure button states match mesh visibility
            updateButtonStates();
            renderLandmarks();
            
            // Mark IOS viewer as initialized
            if (window.IOSViewer && typeof window.IOSViewer.markInitialized === 'function') {
                window.IOSViewer.markInitialized();
            }
        }
    }
    
    // Load upper jaw
    loader.load(upperStlUrl, function(geometry) {
        console.debug('Upper scan loaded successfully, vertices:', geometry.attributes.position.count);
        geometry.computeBoundingBox();
        geometry.computeVertexNormals();
        
        const material = new THREE.MeshPhongMaterial({ 
            color: 0xffcccc,
            side: THREE.DoubleSide
        });
        const mesh = new THREE.Mesh(geometry, material);
        mesh.rotation.y = Math.PI; // Rotate 180 degrees around Y-axis
        
        scene.add(mesh);
        upperMesh1 = mesh;
        renderLandmarks();
        
        console.debug('Upper mesh added to scene');
        onModelLoaded();
    }, undefined, function(error) {
        console.error('Error loading upper jaw:', error);
        if (loadingIndicator) {
            loadingIndicator.innerHTML = '<p style="color: red;">Error loading 3D model</p>';
        }
    });
    
    // Load lower jaw
    loader.load(lowerStlUrl, function(geometry) {
        console.debug('Lower scan loaded successfully, vertices:', geometry.attributes.position.count);
        geometry.computeBoundingBox();
        geometry.computeVertexNormals();
        
        const material = new THREE.MeshPhongMaterial({ 
            color: 0xccccff,
            side: THREE.DoubleSide
        });
        const mesh = new THREE.Mesh(geometry, material);
        mesh.rotation.y = Math.PI; // Rotate 180 degrees around Y-axis
        
        scene.add(mesh);
        lowerMesh1 = mesh;
        renderLandmarks();
        
        console.debug('Lower mesh added to scene');
        onModelLoaded();
    }, undefined, function(error) {
        console.error('Error loading lower jaw:', error);
        if (loadingIndicator) {
            loadingIndicator.innerHTML = '<p style="color: red;">Error loading 3D model</p>';
        }
    });
}

// Center both scans at origin
function centerScansAtOrigin() {
    if (!upperMesh1 || !lowerMesh1) return;
    
    const combinedBox = new THREE.Box3();
    combinedBox.expandByObject(upperMesh1);
    combinedBox.expandByObject(lowerMesh1);
    
    const combinedCenter = combinedBox.getCenter(new THREE.Vector3());
    const offset = combinedCenter.clone().negate();
    
    upperMesh1.position.add(offset);
    lowerMesh1.position.add(offset);
}

// Position camera at appropriate distance
function positionCameraForScans() {
    if (!upperMesh1 || !lowerMesh1 || !camera1 || !controls1) return;
    
    const combinedBox = new THREE.Box3();
    combinedBox.expandByObject(upperMesh1);
    combinedBox.expandByObject(lowerMesh1);
    
    const size = combinedBox.getSize(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z);
    const scaledDistance = Math.max(maxDim * 2, 20);
    
    camera1.position.set(0, scaledDistance, 0);
    camera1.up.set(0, 0, -1);
    camera1.lookAt(0, 0, 0);
    
    controls1.target.set(0, 0, 0);
    controls1.update();
}

// =====================================================
// GRID OVERLAY FUNCTIONALITY
// =====================================================

// Create grid overlay (2D canvas on top of viewer)
function createGrid(size) {
    const container = document.getElementById('scan-viewer');
    if (!container) return;
    
    // Remove existing grid if present
    if (gridOverlay1) {
        gridOverlay1.remove();
    }
    
    // Create canvas element for grid overlay
    gridOverlay1 = document.createElement('canvas');
    gridOverlay1.id = 'grid-overlay';
    gridOverlay1.style.position = 'absolute';
    gridOverlay1.style.top = '0';
    gridOverlay1.style.left = '0';
    gridOverlay1.style.width = '100%';
    gridOverlay1.style.height = '100%';
    gridOverlay1.style.pointerEvents = 'none';
    gridOverlay1.style.display = 'none'; // Hidden by default
    gridOverlay1.style.zIndex = '10';
    
    // Set canvas size to match container
    gridOverlay1.width = container.clientWidth;
    gridOverlay1.height = container.clientHeight;
    
    container.appendChild(gridOverlay1);
    
    // Draw grid
    drawGrid(size);
}

// Draw grid on overlay canvas
function drawGrid(divisions) {
    if (!gridOverlay1) return;
    
    const ctx = gridOverlay1.getContext('2d');
    const width = gridOverlay1.width;
    const height = gridOverlay1.height;
    
    // Clear canvas
    ctx.clearRect(0, 0, width, height);
    
    // Grid styling - slightly darker gray
    ctx.strokeStyle = 'rgba(0, 0, 0, 0.4)';
    ctx.lineWidth = 2;
    
    // Calculate cell size
    const cellWidth = width / divisions;
    const cellHeight = height / divisions;
    
    // Draw vertical lines
    for (let i = 0; i <= divisions; i++) {
        const x = i * cellWidth;
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, height);
        ctx.stroke();
    }
    
    // Draw horizontal lines
    for (let i = 0; i <= divisions; i++) {
        const y = i * cellHeight;
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(width, y);
        ctx.stroke();
    }
}

// Toggle grid visibility
function toggleGrid() {
    if (gridOverlay1) {
        const isVisible = gridOverlay1.style.display !== 'none';
        gridOverlay1.style.display = isVisible ? 'none' : 'block';
        const btn = document.getElementById('toggleGrid');
        if (btn) {
            btn.classList.toggle('active', !isVisible);
        }
    }
}

// Update grid size
function updateGridSize(size) {
    if (gridOverlay1) {
        const wasVisible = gridOverlay1.style.display !== 'none';
        drawGrid(size);
        if (wasVisible) {
            gridOverlay1.style.display = 'block';
        }
    }
}

// Update grid overlay on window resize
function updateGridOnResize() {
    if (gridOverlay1) {
        const container = document.getElementById('scan-viewer');
        if (container) {
            gridOverlay1.width = container.clientWidth;
            gridOverlay1.height = container.clientHeight;
            
            // Redraw grid with current size
            const gridSizeSelect = document.getElementById('gridSize');
            const currentSize = gridSizeSelect ? parseInt(gridSizeSelect.value) : 9;
            drawGrid(currentSize);
        }
    }
}

// =====================================================
// LANDMARK ANNOTATION
// =====================================================

function landmarkApiUrl() {
    return `/${window.projectNamespace}/api/patient/${window.scanId}/ios/landmarks/`;
}

function getCsrfToken() {
    const input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (input && input.value) return input.value;
    const cookie = document.cookie.split('; ').find(row => row.startsWith('csrftoken='));
    return cookie ? decodeURIComponent(cookie.split('=')[1]) : '';
}

function canEditLandmarks() {
    return Boolean(window.canEdit);
}

function setLandmarkStatus(message) {
    const status = document.getElementById('iosLandmarkStatus');
    if (status) status.textContent = message;
}

function cloneLandmarks() {
    return JSON.parse(JSON.stringify(landmarkState1.landmarks || {}));
}

function pushLandmarkUndo() {
    landmarkState1.undoStack.push(cloneLandmarks());
    if (landmarkState1.undoStack.length > 50) landmarkState1.undoStack.shift();
}

function landmarkCountForTooth(tooth) {
    const jaw = ['1', '2'].includes(tooth[0]) ? 'upper' : 'lower';
    const entry = landmarkState1.landmarks[`${window.scanId}_${jaw}_FDI_${tooth}`] || {};
    return landmarkTypes1.reduce((count, type) => {
        if (['cusps', 'planar'].includes(type)) return count + (Array.isArray(entry[type]) ? entry[type].length : 0);
        return count + (Array.isArray(entry[type]) && entry[type].length === 3 ? 1 : 0);
    }, 0);
}

function renderLandmarkTeeth() {
    const container = document.getElementById('iosLandmarkTeeth');
    if (!container) return;
    container.querySelectorAll('.ios-landmark-tooth').forEach(button => {
        const count = landmarkCountForTooth(button.dataset.tooth);
        button.classList.toggle('active', button.dataset.tooth === landmarkState1.selectedTooth);
        button.classList.toggle('has-landmarks', count > 0);
        button.dataset.count = String(count);
        button.setAttribute('aria-pressed', String(button.dataset.tooth === landmarkState1.selectedTooth));
    });
}

function currentLandmarkInstruction() {
    if (!canEditLandmarks()) return 'Viewing saved landmarks';
    if (!landmarkState1.selectedTooth) return 'Select an FDI tooth';
    if (!landmarkState1.selectedType) return `Tooth ${landmarkState1.selectedTooth} · Select a landmark type`;
    return `Tooth ${landmarkState1.selectedTooth} · ${landmarkTypeLabels1[landmarkState1.selectedType]} · Shift + left-click to place`;
}

function updateLandmarkControls() {
    const modeButton = document.getElementById('toggleLandmarkMode');
    const workbench = document.getElementById('iosLandmarkWorkbench');
    const saveButton = document.getElementById('saveLandmarks');
    const placeButton = document.getElementById('landmarkPlaceTool');
    const selectButton = document.getElementById('landmarkSelectTool');
    const undoButton = document.getElementById('undoLandmark');
    const deleteButton = document.getElementById('deleteLandmark');
    const editable = canEditLandmarks();
    if (modeButton) {
        modeButton.classList.toggle('active', landmarkState1.active);
        modeButton.setAttribute('aria-pressed', String(landmarkState1.active));
        modeButton.setAttribute('aria-expanded', String(landmarkState1.active));
    }
    if (workbench) workbench.hidden = !landmarkState1.active;
    document.querySelectorAll('.ios-landmark-tooth').forEach(button => { button.disabled = !editable; });
    document.querySelectorAll('.ios-landmark-type').forEach(button => {
        button.disabled = !editable;
        button.classList.toggle('active', button.dataset.landmarkType === landmarkState1.selectedType);
    });
    if (placeButton) {
        placeButton.disabled = !editable;
        placeButton.classList.toggle('active', landmarkState1.tool === 'place');
        placeButton.setAttribute('aria-pressed', String(landmarkState1.tool === 'place'));
    }
    if (selectButton) {
        selectButton.classList.toggle('active', landmarkState1.tool === 'select');
        selectButton.setAttribute('aria-pressed', String(landmarkState1.tool === 'select'));
    }
    if (undoButton) undoButton.disabled = !editable || !landmarkState1.undoStack.length;
    if (deleteButton) deleteButton.disabled = !editable || !landmarkState1.selectedMarker || landmarkState1.selectedMarker.type === 'planar';
    if (saveButton) saveButton.disabled = !editable || !landmarkState1.dirty;
    if (renderer1) renderer1.domElement.style.cursor = landmarkState1.active && landmarkState1.tool === 'place' ? 'crosshair' : '';
    renderLandmarkTeeth();
}

function toggleLandmarkVisibility() {
    landmarkState1.showLandmarks = !landmarkState1.showLandmarks;
    const button = document.getElementById('toggleLandmarkVisibility');
    if (button) {
        button.classList.toggle('active', landmarkState1.showLandmarks);
        button.setAttribute('aria-pressed', String(landmarkState1.showLandmarks));
        const icon = button.querySelector('i');
        if (icon) {
            icon.classList.toggle('fa-eye', landmarkState1.showLandmarks);
            icon.classList.toggle('fa-eye-slash', !landmarkState1.showLandmarks);
        }
    }
    if (landmarkState1.showLandmarks) setLandmarkStatus('Showing saved landmarks');
    renderLandmarks();
}

function setLandmarkTypeVisibility(type, visible) {
    landmarkState1.visibleTypes[type] = visible;
    // Keep both toggle surfaces (workbench + toolbar dropdown) in sync.
    document.querySelectorAll('.ios-landmark-vis[data-landmark-type="' + type + '"]').forEach(input => {
        input.checked = visible;
    });
    syncLandmarkVisibility();
}

function initLandmarkVisibilityControls() {
    ['iosLandmarkVisibilityWorkbench', 'iosLandmarkVisibilityDropdown'].forEach(id => {
        const container = document.getElementById(id);
        if (!container || container.dataset.initialized) return;
        container.dataset.initialized = 'true';
        landmarkTypes1.forEach(type => {
            const label = document.createElement('label');
            label.className = 'ios-landmark-vis-item';
            const input = document.createElement('input');
            input.type = 'checkbox';
            input.className = 'ios-landmark-vis';
            input.dataset.landmarkType = type;
            input.checked = isLandmarkTypeVisible(type);
            input.setAttribute('aria-label', 'Show ' + (landmarkTypeLabels1[type] || type) + ' landmarks');
            const dot = document.createElement('span');
            dot.className = 'ios-landmark-vis-dot';
            dot.style.setProperty('--landmark-color', '#' + landmarkColors1[type].toString(16).padStart(6, '0'));
            const text = document.createElement('span');
            text.textContent = landmarkTypeLabels1[type] || type;
            input.addEventListener('change', function() {
                setLandmarkTypeVisibility(type, input.checked);
            });
            label.appendChild(input);
            label.appendChild(dot);
            label.appendChild(text);
            container.appendChild(label);
        });
    });
}

function initLandmarkControls() {
    const teeth = document.getElementById('iosLandmarkTeeth');
    const types = document.getElementById('iosLandmarkTypes');
    if (!teeth || !types || teeth.dataset.initialized) return;
    teeth.dataset.initialized = 'true';
    landmarkTeeth1.forEach(tooth => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'ios-landmark-tooth';
        button.dataset.tooth = tooth;
        button.textContent = tooth;
        button.setAttribute('aria-label', `Tooth ${tooth}`);
        button.addEventListener('click', function() {
            landmarkState1.selectedTooth = tooth;
            landmarkState1.selectedMarker = null;
            setLandmarkStatus(currentLandmarkInstruction());
            updateLandmarkControls();
            renderLandmarks();
        });
        teeth.appendChild(button);
    });
    editableLandmarkTypes1.forEach(type => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'ios-landmark-type';
        button.dataset.landmarkType = type;
        button.textContent = landmarkTypeLabels1[type];
        button.style.setProperty('--landmark-color', `#${landmarkColors1[type].toString(16).padStart(6, '0')}`);
        button.addEventListener('click', function() {
            landmarkState1.selectedType = type;
            landmarkState1.tool = 'place';
            landmarkState1.selectedMarker = null;
            setLandmarkStatus(currentLandmarkInstruction());
            updateLandmarkControls();
            renderLandmarks();
        });
        types.appendChild(button);
    });
    const visButton = document.getElementById('toggleLandmarkVisibility');
    if (visButton) visButton.addEventListener('click', toggleLandmarkVisibility);
    initLandmarkVisibilityControls();
    document.getElementById('toggleLandmarkMode').addEventListener('click', function() {
        landmarkState1.active = !landmarkState1.active;
        landmarkState1.selectedMarker = null;
        renderLandmarks();
        if (landmarkState1.active) setLandmarkStatus(currentLandmarkInstruction());
        updateLandmarkControls();
    });
    document.getElementById('landmarkPlaceTool').addEventListener('click', function() {
        landmarkState1.tool = 'place';
        landmarkState1.selectedMarker = null;
        setLandmarkStatus(currentLandmarkInstruction());
        updateLandmarkControls();
        renderLandmarks();
    });
    document.getElementById('landmarkSelectTool').addEventListener('click', function() {
        landmarkState1.tool = 'select';
        setLandmarkStatus('Click a marker to select it');
        updateLandmarkControls();
    });
    document.getElementById('undoLandmark').addEventListener('click', undoLandmarkChange);
    document.getElementById('deleteLandmark').addEventListener('click', deleteSelectedLandmark);
    document.getElementById('saveLandmarks').addEventListener('click', saveLandmarks);
    initLandmarkDisplayControls();
    document.addEventListener('keydown', onLandmarkKeyDown);
    updateLandmarkControls();
}

function initLandmarkDisplayControls() {
    const sizeRange = document.getElementById('landmarkSizeRange');
    const sizeValue = document.getElementById('landmarkSizeValue');
    if (sizeRange) {
        sizeRange.value = String(landmarkState1.markerSize);
        sizeRange.addEventListener('input', function() {
            landmarkState1.markerSize = parseFloat(this.value);
            if (sizeValue) sizeValue.textContent = this.value;
            renderLandmarks();
        });
    }
    const axisToggle = document.getElementById('toggleAxis');
    if (axisToggle) {
        axisToggle.checked = landmarkState1.showAxis;
        axisToggle.addEventListener('change', function() {
            landmarkState1.showAxis = this.checked;
            if (referenceAxis1) referenceAxis1.visible = this.checked;
        });
    }
    const whiteToggle = document.getElementById('toggleWhiteBackground');
    if (whiteToggle) {
        whiteToggle.checked = landmarkState1.whiteBackground;
        whiteToggle.addEventListener('change', function() {
            landmarkState1.whiteBackground = this.checked;
            if (scene1) scene1.background = iosSceneBackgroundColor();
        });
    }
}

function renderLandmarks() {
    if (!landmarkMarkers1) return;
    landmarkMarkers1.children.slice().forEach(marker => {
        landmarkMarkers1.remove(marker);
        marker.geometry.dispose();
        marker.material.dispose();
    });
    if ((!landmarkState1.active && !landmarkState1.showLandmarks) || (!upperMesh1 && !lowerMesh1)) return;
    if (upperMesh1) upperMesh1.updateWorldMatrix(true, false);
    if (lowerMesh1) lowerMesh1.updateWorldMatrix(true, false);
    Object.entries(landmarkState1.landmarks || {}).forEach(([key, entry]) => {
        const match = /^(\d+)_(upper|lower)_FDI_(\d{2})$/.exec(key);
        if (!match || match[1] !== String(window.scanId) || !entry || typeof entry !== 'object') return;
        const mesh = match[2] === 'upper' ? upperMesh1 : lowerMesh1;
        if (!mesh) return;
        landmarkTypes1.forEach(type => {
            const values = ['cusps', 'planar'].includes(type) ? entry[type] : [entry[type]];
            if (!Array.isArray(values)) return;
            values.forEach((value, index) => {
                if (!Array.isArray(value) || value.length !== 3) return;
                const marker = new THREE.Mesh(
                    new THREE.SphereGeometry(landmarkState1.markerSize, 16, 12),
                    new THREE.MeshBasicMaterial({ color: landmarkColors1[type] || 0xffffff, depthTest: true, transparent: true, opacity: 0.92 })
                );
                marker.position.copy(mesh.localToWorld(new THREE.Vector3(value[0], value[1], value[2])));
                marker.userData.landmark = { key, type, index: ['cusps', 'planar'].includes(type) ? index : null, tooth: match[3] };
                if (landmarkState1.selectedMarker && landmarkState1.selectedMarker.key === key && landmarkState1.selectedMarker.type === type && landmarkState1.selectedMarker.index === marker.userData.landmark.index) {
                    marker.scale.setScalar(1.45);
                    marker.material.opacity = 1;
                }
                marker.renderOrder = 2;
                landmarkMarkers1.add(marker);
            });
        });
    });
    syncLandmarkVisibility();
}

// Hide landmark markers for a jaw whose mesh is hidden, so toggling the
// upper/lower arch buttons also toggles that jaw's landmarks. Also applies the
// view-only visibility flag and the per-type visibility toggles.
function syncLandmarkVisibility() {
    if (!landmarkMarkers1) return;
    const anyVisible = landmarkState1.active || landmarkState1.showLandmarks;
    landmarkMarkers1.children.forEach(marker => {
        const data = marker.userData.landmark;
        if (!data || !data.key) return;
        const isUpper = /^(\d+)_upper_FDI_/.test(data.key);
        const mesh = isUpper ? upperMesh1 : lowerMesh1;
        marker.visible = anyVisible && (!mesh || mesh.visible) && isLandmarkTypeVisible(data.type);
    });
}

function raycastLandmarkEvent(event, objects) {
    if (!camera1 || !renderer1) return null;
    const rect = renderer1.domElement.getBoundingClientRect();
    const pointer = new THREE.Vector2(
        ((event.clientX - rect.left) / rect.width) * 2 - 1,
        -((event.clientY - rect.top) / rect.height) * 2 + 1
    );
    const raycaster = new THREE.Raycaster();
    raycaster.setFromCamera(pointer, camera1);
    return raycaster.intersectObjects(objects, false)[0] || null;
}

function onLandmarkPointerDown(event) {
    if (!landmarkState1.active || event.button !== 0) return;
    if (landmarkState1.tool === 'select' && landmarkMarkers1) {
        const markerHit = raycastLandmarkEvent(event, landmarkMarkers1.children);
        if (!markerHit) {
            landmarkState1.selectedMarker = null;
            updateLandmarkControls();
            renderLandmarks();
            return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        landmarkState1.selectedMarker = { ...markerHit.object.userData.landmark };
        landmarkState1.selectedTooth = landmarkState1.selectedMarker.tooth;
        const readOnlySuffix = landmarkState1.selectedMarker.type === 'planar' ? ' · Read-only' : '';
        setLandmarkStatus(`Selected tooth ${landmarkState1.selectedTooth} · ${landmarkTypeLabels1[landmarkState1.selectedMarker.type] || landmarkState1.selectedMarker.type}${readOnlySuffix}`);
        updateLandmarkControls();
        renderLandmarks();
        return;
    }
    if (landmarkState1.tool !== 'place' || !event.shiftKey) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (!canEditLandmarks()) return;
    if (!landmarkState1.selectedTooth || !landmarkState1.selectedType) {
        setLandmarkStatus('Select a tooth and landmark type');
        return;
    }
    if (!camera1 || !renderer1 || (!upperMesh1 && !lowerMesh1)) return;
    if (upperMesh1) upperMesh1.updateWorldMatrix(true, false);
    if (lowerMesh1) lowerMesh1.updateWorldMatrix(true, false);
    const hit = raycastLandmarkEvent(event, [upperMesh1, lowerMesh1].filter(mesh => mesh && mesh.visible));
    if (!hit) {
        setLandmarkStatus('Click a visible scan surface');
        return;
    }
    const expectedJaw = ['1', '2'].includes(landmarkState1.selectedTooth[0]) ? 'upper' : 'lower';
    const hitJaw = hit.object === upperMesh1 ? 'upper' : 'lower';
    if (expectedJaw !== hitJaw) {
        setLandmarkStatus(`Select the ${hitJaw} jaw tooth`);
        return;
    }
    const localPoint = hit.object.worldToLocal(hit.point.clone());
    const key = `${window.scanId}_${expectedJaw}_FDI_${landmarkState1.selectedTooth}`;
    const entry = landmarkState1.landmarks[key] || {};
    const point = [localPoint.x, localPoint.y, localPoint.z];
    pushLandmarkUndo();
    if (['cusps', 'planar'].includes(landmarkState1.selectedType)) {
        entry[landmarkState1.selectedType] = Array.isArray(entry[landmarkState1.selectedType]) ? entry[landmarkState1.selectedType] : [];
        entry[landmarkState1.selectedType].push(point);
    } else {
        entry[landmarkState1.selectedType] = point;
    }
    landmarkState1.landmarks[key] = entry;
    landmarkState1.selectedMarker = null;
    landmarkState1.dirty = true;
    setLandmarkStatus(`Placed ${landmarkTypeLabels1[landmarkState1.selectedType]} on tooth ${landmarkState1.selectedTooth} · Unsaved`);
    renderLandmarks();
    updateLandmarkControls();
}

function deleteSelectedLandmark() {
    const selected = landmarkState1.selectedMarker;
    if (!selected || selected.type === 'planar' || !canEditLandmarks()) return;
    const entry = landmarkState1.landmarks[selected.key];
    if (!entry) return;
    pushLandmarkUndo();
    if (selected.index === null) {
        delete entry[selected.type];
    } else if (Array.isArray(entry[selected.type])) {
        entry[selected.type].splice(selected.index, 1);
        if (!entry[selected.type].length) delete entry[selected.type];
    }
    if (!Object.keys(entry).length) delete landmarkState1.landmarks[selected.key];
    landmarkState1.selectedMarker = null;
    landmarkState1.dirty = true;
    setLandmarkStatus('Landmark deleted · Unsaved');
    renderLandmarks();
    updateLandmarkControls();
}

function undoLandmarkChange() {
    if (!landmarkState1.undoStack.length || !canEditLandmarks()) return;
    landmarkState1.landmarks = landmarkState1.undoStack.pop();
    landmarkState1.selectedMarker = null;
    landmarkState1.dirty = true;
    setLandmarkStatus('Last change undone · Unsaved');
    renderLandmarks();
    updateLandmarkControls();
}

function onLandmarkKeyDown(event) {
    if (!landmarkState1.active || ['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement && document.activeElement.tagName)) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
        event.preventDefault();
        undoLandmarkChange();
    } else if ((event.key === 'Delete' || event.key === 'Backspace') && landmarkState1.selectedMarker) {
        event.preventDefault();
        deleteSelectedLandmark();
    } else if (event.key === 'Escape') {
        landmarkState1.selectedMarker = null;
        setLandmarkStatus(currentLandmarkInstruction());
        renderLandmarks();
        updateLandmarkControls();
    }
}

function loadLandmarks() {
    fetch(landmarkApiUrl())
        .then(response => response.ok ? response.json() : Promise.reject(new Error('Unable to load landmarks')))
        .then(data => {
            landmarkState1.landmarks = data.landmarks && typeof data.landmarks === 'object' ? data.landmarks : {};
            landmarkState1.undoStack = [];
            landmarkState1.dirty = false;
            renderLandmarks();
            updateLandmarkControls();
        })
        .catch(error => {
            setLandmarkStatus('Landmarks could not be loaded');
            console.error('Error loading IOS landmarks:', error);
        });
}

function saveLandmarks() {
    if (!landmarkState1.dirty || !canEditLandmarks()) return;
    setLandmarkStatus('Saving...');
    const headers = { 'Content-Type': 'application/json' };
    const csrfToken = getCsrfToken();
    if (csrfToken) headers['X-CSRFToken'] = csrfToken;
    fetch(landmarkApiUrl(), {
        method: 'PUT',
        headers,
        body: JSON.stringify({ landmarks: landmarkState1.landmarks })
    })
        .then(async response => {
            const body = await response.text();
            let data = {};
            if (body) {
                try {
                    data = JSON.parse(body);
                } catch (error) {
                    if (response.ok) throw new Error('The server returned an invalid save response');
                }
            }
            if (!response.ok) throw new Error(data.error || `Unable to save landmarks (HTTP ${response.status})`);
            return data;
        })
        .then(() => {
            landmarkState1.dirty = false;
            landmarkState1.undoStack = [];
            setLandmarkStatus('Saved');
            updateLandmarkControls();
            if (typeof window.appNotify === 'function') window.appNotify('success', 'Landmarks saved');
        })
        .catch(error => {
            setLandmarkStatus('Save failed');
            console.error('Error saving IOS landmarks:', error);
            if (typeof window.appNotify === 'function') window.appNotify('danger', error.message);
        });
}

// =====================================================
// CAMERA POSITIONING FUNCTIONS
// =====================================================

// View from the right side
function viewRight() {
    if (!camera1 || !controls1) return;
    
    const distance = camera1.position.length();
    camera1.position.set(-distance, 0, 0);
    camera1.up.set(0, 0, -1);
    camera1.lookAt(0, 0, 0);
    
    controls1.target.set(0, 0, 0);
    controls1.update();
}

// View from the left side
function viewLeft() {
    if (!camera1 || !controls1) return;
    
    const distance = camera1.position.length();
    camera1.position.set(distance, 0, 0);
    camera1.up.set(0, 0, -1);
    camera1.lookAt(0, 0, 0);
    
    controls1.target.set(0, 0, 0);
    controls1.update();
}

// View from the front (same as reset)
function viewFront() {
    if (!camera1 || !controls1) return;
    
    const distance = camera1.position.length();
    camera1.position.set(0, distance, 0);
    camera1.up.set(0, 0, -1);
    camera1.lookAt(0, 0, 0);
    
    controls1.target.set(0, 0, 0);
    controls1.update();
}

// View upper scan - hide lower, show upper, camera positioned for axis 0 left, axis 1 up, axis 2 towards screen
function viewUpper() {
    if (!camera1 || !controls1 || !upperMesh1 || !lowerMesh1) return;
    
    // Hide lower scan, show upper scan
    lowerMesh1.visible = false;
    upperMesh1.visible = true;
    
    // Position camera for upper scan view
    // Axis 0 to the left, axis 1 up, axis 2 towards screen
    const distance = camera1.position.length();
    camera1.position.set(0, 0, distance); // Looking down from above
    camera1.up.set(0, 1, -1); // Z-axis as up
    camera1.lookAt(0, 0, 0);
    
    controls1.target.set(0, 0, 0);
    controls1.update();
    
    // Update button states
    updateButtonStates();
    syncLandmarkVisibility();
}

// View lower scan - hide upper, show lower, camera positioned for axis 0 left, axis 1 down, axis 2 out from screen
function viewLower() {
    if (!camera1 || !controls1 || !upperMesh1 || !lowerMesh1) return;
    
    // Hide upper scan, show lower scan
    upperMesh1.visible = false;
    lowerMesh1.visible = true;
    
    // Position camera for lower scan view
    // Axis 0 to the left, axis 1 down, axis 2 out from screen
    const distance = camera1.position.length();
    camera1.position.set(0, 0, -distance); // Looking up from below
    camera1.up.set(0, -1, 1); // Z-axis as up (inverted)
    camera1.lookAt(0, 0, 0);
    
    controls1.target.set(0, 0, 0);
    controls1.update();
    
    // Update button states
    updateButtonStates();
    syncLandmarkVisibility();
}

// Animation loop
function animate() {
    requestAnimationFrame(animate);
    
    if (controls1) {
        controls1.update();
    }
    
    if (cameraLight1) {
        cameraLight1.position.copy(camera1.position);
    }
    
    if (renderer1 && scene1 && camera1) {
        renderer1.render(scene1, camera1);
    }
}

// Initialize the STL viewer
function initSTLViewers(upperStlUrl, lowerStlUrl) {
    initViewer('scan-viewer', upperStlUrl, lowerStlUrl);
    window.addEventListener('resize', onWindowResize);
}

// =====================================================
// UI CONTROLS
// =====================================================

// 3D Viewer button controls
function toggleMesh(type) {
    if (type === 'upper' && upperMesh1) {
        upperMesh1.visible = !upperMesh1.visible;
    } else if (type === 'lower' && lowerMesh1) {
        lowerMesh1.visible = !lowerMesh1.visible;
    }
    syncLandmarkVisibility();
}

function resetView() {
    if (camera1 && controls1) {
        camera1.position.set(0, 80, 0);
        camera1.up.set(0, 0, -1);
        camera1.lookAt(0, 0, 0);
        controls1.target.set(0, 0, 0);
        controls1.reset();
        controls1.update();
    }
}

// Handle window resize
function onWindowResize() {
    const container = document.getElementById('scan-viewer');
    
    if (container && camera1 && renderer1 && controls1) {
        camera1.aspect = container.clientWidth / container.clientHeight;
        camera1.updateProjectionMatrix();
        renderer1.setSize(container.clientWidth, container.clientHeight);
        
        controls1.screen.width = container.clientWidth;
        controls1.screen.height = container.clientHeight;
        controls1.handleResize();
        
        // Update grid overlay
        updateGridOnResize();
    }
}

// Update button states to match mesh visibility
function updateButtonStates() {
    // Update upper jaw button
    const showUpperBtn = document.getElementById('showUpper');
    if (showUpperBtn && upperMesh1) {
        showUpperBtn.classList.toggle('active', upperMesh1.visible);
        const icon = showUpperBtn.querySelector('i');
        if (icon) {
            if (upperMesh1.visible) {
                icon.classList.remove('fa-eye-slash');
                icon.classList.add('fa-eye');
            } else {
                icon.classList.remove('fa-eye');
                icon.classList.add('fa-eye-slash');
            }
        }
    }
    
    // Update lower jaw button
    const showLowerBtn = document.getElementById('showLower');
    if (showLowerBtn && lowerMesh1) {
        showLowerBtn.classList.toggle('active', lowerMesh1.visible);
        const icon = showLowerBtn.querySelector('i');
        if (icon) {
            if (lowerMesh1.visible) {
                icon.classList.remove('fa-eye-slash');
                icon.classList.add('fa-eye');
            } else {
                icon.classList.remove('fa-eye');
                icon.classList.add('fa-eye-slash');
            }
        }
    }
}

// Initialize 3D viewer control buttons
function init3DControls() {
    // Reset view button
    const resetViewBtn = document.getElementById('resetView');
    if (resetViewBtn) {
        resetViewBtn.addEventListener('click', resetView);
    }

    // Toggle wireframe button
    const toggleWireframeBtn = document.getElementById('toggleWireframe');
    if (toggleWireframeBtn) {
        toggleWireframeBtn.addEventListener('click', function() {
            if (upperMesh1) upperMesh1.material.wireframe = !upperMesh1.material.wireframe;
            if (lowerMesh1) lowerMesh1.material.wireframe = !lowerMesh1.material.wireframe;
        });
    }

    // Toggle grid button
    const toggleGridBtn = document.getElementById('toggleGrid');
    if (toggleGridBtn) {
        toggleGridBtn.addEventListener('click', toggleGrid);
    }

    // Grid size selector
    const gridSizeSelect = document.getElementById('gridSize');
    if (gridSizeSelect) {
        gridSizeSelect.addEventListener('change', function() {
            updateGridSize(parseInt(this.value));
        });
    }

    initLandmarkControls();

    // Toggle upper jaw visibility
    const showUpperBtn = document.getElementById('showUpper');
    if (showUpperBtn) {
        showUpperBtn.addEventListener('click', function() {
            console.debug('Upper jaw button clicked, upperMesh1:', upperMesh1);
            if (upperMesh1) {
                upperMesh1.visible = !upperMesh1.visible;
                this.classList.toggle('active', upperMesh1.visible);
                
                // Update icon
                const icon = this.querySelector('i');
                if (icon) {
                    if (upperMesh1.visible) {
                        icon.classList.remove('fa-eye-slash');
                        icon.classList.add('fa-eye');
                    } else {
                        icon.classList.remove('fa-eye');
                        icon.classList.add('fa-eye-slash');
                    }
                }
                console.debug('Upper jaw visibility toggled to:', upperMesh1.visible);
                syncLandmarkVisibility();
            } else {
                console.warn('Upper mesh not loaded yet');
            }
        });
    }

    // Toggle lower jaw visibility
    const showLowerBtn = document.getElementById('showLower');
    if (showLowerBtn) {
        showLowerBtn.addEventListener('click', function() {
            console.debug('Lower jaw button clicked, lowerMesh1:', lowerMesh1);
            if (lowerMesh1) {
                lowerMesh1.visible = !lowerMesh1.visible;
                this.classList.toggle('active', lowerMesh1.visible);
                
                // Update icon
                const icon = this.querySelector('i');
                if (icon) {
                    if (lowerMesh1.visible) {
                        icon.classList.remove('fa-eye-slash');
                        icon.classList.add('fa-eye');
                    } else {
                        icon.classList.remove('fa-eye');
                        icon.classList.add('fa-eye-slash');
                    }
                }
                console.debug('Lower jaw visibility toggled to:', lowerMesh1.visible);
                syncLandmarkVisibility();
            } else {
                console.warn('Lower mesh not loaded yet');
            }
        });
    }

    // View positioning buttons
    const viewRightBtn = document.getElementById('viewRight');
    if (viewRightBtn) {
        viewRightBtn.addEventListener('click', viewRight);
    }

    const viewLeftBtn = document.getElementById('viewLeft');
    if (viewLeftBtn) {
        viewLeftBtn.addEventListener('click', viewLeft);
    }

    const viewFrontBtn = document.getElementById('viewFront');
    if (viewFrontBtn) {
        viewFrontBtn.addEventListener('click', viewFront);
    }

    // View upper scan button
    const viewUpperBtn = document.getElementById('viewUpper');
    if (viewUpperBtn) {
        viewUpperBtn.addEventListener('click', viewUpper);
    }

    // View lower scan button
    const viewLowerBtn = document.getElementById('viewLower');
    if (viewLowerBtn) {
        viewLowerBtn.addEventListener('click', viewLower);
    }
}

// Load scan data from API and initialize viewer
function loadScanDataAndInitViewer() {
    console.debug('Loading scan data for ID:', window.scanId);
    console.debug('Project namespace:', window.projectNamespace);
    
    const apiUrl = `/${window.projectNamespace}/api/patient/${window.scanId}/data/`;
    console.debug('Fetching from:', apiUrl);
    
    fetch(apiUrl)
        .then(async response => {
            console.debug('Response status:', response.status);
            if (response.status === 202) {
                // Processing in progress
                const data = await response.json();
                throw new Error(`processing:${data.message || 'IOS scans are being processed'}`);
            }
            if (!response.ok) {
                // Try to get error details
                try {
                    const errorData = await response.json();
                    if (errorData.status === 'processing') {
                        throw new Error(`processing:${errorData.message}`);
                    } else if (errorData.status === 'failed') {
                        throw new Error(`failed:${errorData.message}`);
                    }
                } catch (e) {
                    // If JSON parsing fails, use generic error
                }
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            console.debug('Scan data received:', data);
            if (data.error) {
                console.error('Error loading scan data:', data.error);
                return;
            }
            
            console.debug('Upper scan URL:', data.upper_scan_url);
            console.debug('Lower scan URL:', data.lower_scan_url);
            
            // Initialize the STL viewer with the scan URLs
            initSTLViewers(data.upper_scan_url, data.lower_scan_url);
            loadLandmarks();
        })
        .catch(error => {
            console.error('Error fetching scan data:', error);
            
            // Show appropriate message in the viewer
            const viewerContainer = document.getElementById('iosViewerContainer');
            if (viewerContainer) {
                let message = 'Failed to load scan data';
                let iconClass = 'fa-exclamation-triangle text-warning';
                let textClass = 'text-muted';
                
                if (error.message.startsWith('processing:')) {
                    message = error.message.substring('processing:'.length);
                    iconClass = 'fa-spinner fa-spin text-info';
                    textClass = 'text-info';
                } else if (error.message.startsWith('failed:')) {
                    message = error.message.substring('failed:'.length);
                    iconClass = 'fa-times-circle text-danger';
                    textClass = 'text-danger';
                }
                
                viewerContainer.innerHTML = `
                    <div class="text-center py-5">
                        <i class="fas ${iconClass} mb-3" style="font-size: 3rem;"></i>
                        <p class="${textClass}">${message}</p>
                    </div>
                `;
            }
        });
}

// Export IOSViewer module
window.IOSViewer = {
    initialized: false,
    loading: false,
    
    init: function() {
        console.debug('IOS Viewer init called');
        console.debug('window.hasIOS:', window.hasIOS);
        console.debug('window.scanId:', window.scanId);
        console.debug('THREE available:', typeof THREE !== 'undefined');
        
        // Check if the scan-viewer container exists
        const container = document.getElementById('scan-viewer');
        const parentContainer = document.getElementById('ios-viewer');
        console.debug('scan-viewer container:', container);
        console.debug('ios-viewer parent container:', parentContainer);
        
        // Check if Three.js is available
        if (typeof THREE === 'undefined') {
            console.error('Three.js is not loaded!');
            return;
        }
        
        // If already initialized, just make sure viewer is visible
        if (this.initialized) {
            console.debug('IOS Viewer already initialized');
            return;
        }
        
        // If currently loading, don't start again
        if (this.loading) {
            console.debug('IOS Viewer already loading');
            return;
        }
        
        // Check if container exists
        if (!container) {
            console.warn('scan-viewer container not found - IOS scans may not be available');
            return;
        }
        
        // Make sure parent container is visible
        if (parentContainer && parentContainer.style.display === 'none') {
            console.debug('Parent ios-viewer is hidden, making it visible');
            parentContainer.style.display = 'block';
        }
        
        // Load IOS scan data only if IOS exists
        if (window.hasIOS) {
            console.debug('Loading IOS scan data because hasIOS is true');
            this.loading = true;
            loadScanDataAndInitViewer();
        } else {
            console.debug('Skipping IOS scan data load because hasIOS is false');
        }
        
        // Initialize 3D controls (safe to call multiple times)
        init3DControls();
    },
    
    // Mark as initialized after successful load
    markInitialized: function() {
        this.initialized = true;
        this.loading = false;
        console.debug('IOS Viewer marked as initialized');
    },
    
    // Expose utility functions
    toggleMesh: toggleMesh,
    resetView: resetView,
    toggleGrid: toggleGrid,
    updateGridSize: updateGridSize,
    updateButtonStates: updateButtonStates,
    viewRight: viewRight,
    viewLeft: viewLeft,
    viewFront: viewFront,
    viewUpper: viewUpper,
    viewLower: viewLower,
    loadLandmarks: loadLandmarks,
    saveLandmarks: saveLandmarks
};
