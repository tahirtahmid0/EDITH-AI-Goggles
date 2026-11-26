/*
  ============================================================
                 EDITH - CAMERA FIRMWARE V1.3
  ============================================================

  Board:       AI-Thinker ESP32-CAM
  Sensor:      OV2640
  Stream:      http://<CAMERA_IP>:81/stream
  Capture:     http://<CAMERA_IP>/capture
  Serial:      115200 baud

  IMPORTANT:
  1. Enter your own Wi-Fi SSID and password below.
  2. Do not share this sketch with real Wi-Fi credentials in it.
  3. Select "AI Thinker ESP32-CAM" in Arduino IDE.
  4. Enable PSRAM if your Arduino board menu exposes that option.

  This is a reconstructed V1.3 based on the known EDITH V1.1
  camera firmware and the later EDITH camera endpoints. It is
  not a byte-for-byte recovery of the missing original V1.3.
*/

#include "esp_camera.h"
#include <WiFi.h>
#include "esp_http_server.h"

// ============================================================
// WIFI CONFIGURATION
// ============================================================

const char* WIFI_SSID = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

const unsigned long WIFI_RETRY_INTERVAL = 10000;

bool wifiWasConnected = false;
unsigned long lastWifiAttempt = 0;

// ============================================================
// AI-THINKER ESP32-CAM PIN CONFIGURATION
// ============================================================

#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27

#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5

#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

// ============================================================
// HTTP SERVER HANDLES
// ============================================================

httpd_handle_t captureServer = NULL;
httpd_handle_t streamServer = NULL;

// MJPEG stream multipart definitions.
static const char* STREAM_CONTENT_TYPE =
    "multipart/x-mixed-replace;boundary=frame";

static const char* STREAM_BOUNDARY =
    "\r\n--frame\r\n";

static const char* STREAM_PART_HEADER =
    "Content-Type: image/jpeg\r\n"
    "Content-Length: %u\r\n\r\n";

// ============================================================
// CAMERA INITIALIZATION
// ============================================================

bool startCamera() {
    camera_config_t config = {};

    config.ledc_channel = LEDC_CHANNEL_0;
    config.ledc_timer = LEDC_TIMER_0;

    config.pin_d0 = Y2_GPIO_NUM;
    config.pin_d1 = Y3_GPIO_NUM;
    config.pin_d2 = Y4_GPIO_NUM;
    config.pin_d3 = Y5_GPIO_NUM;
    config.pin_d4 = Y6_GPIO_NUM;
    config.pin_d5 = Y7_GPIO_NUM;
    config.pin_d6 = Y8_GPIO_NUM;
    config.pin_d7 = Y9_GPIO_NUM;

    config.pin_xclk = XCLK_GPIO_NUM;
    config.pin_pclk = PCLK_GPIO_NUM;
    config.pin_vsync = VSYNC_GPIO_NUM;
    config.pin_href = HREF_GPIO_NUM;

    // Compatible with current Arduino-ESP32 camera driver.
    config.pin_sccb_sda = SIOD_GPIO_NUM;
    config.pin_sccb_scl = SIOC_GPIO_NUM;

    config.pin_pwdn = PWDN_GPIO_NUM;
    config.pin_reset = RESET_GPIO_NUM;

    config.xclk_freq_hz = 20000000;
    config.pixel_format = PIXFORMAT_JPEG;

    const bool hasPSRAM = psramFound();

    Serial.println();
    Serial.println("[CAMERA] Initialising AI-Thinker ESP32-CAM...");

    if (hasPSRAM) {
        Serial.printf(
            "[CAMERA] PSRAM detected: %u bytes\n",
            ESP.getPsramSize()
        );

        config.frame_size = FRAMESIZE_VGA;
        config.jpeg_quality = 10;
        config.fb_count = 2;
        config.fb_location = CAMERA_FB_IN_PSRAM;
        config.grab_mode = CAMERA_GRAB_LATEST;
    } else {
        Serial.println("[CAMERA WARNING] PSRAM not detected.");
        Serial.println("[CAMERA] Using conservative framebuffer settings.");

        config.frame_size = FRAMESIZE_VGA;
        config.jpeg_quality = 12;
        config.fb_count = 1;
        config.fb_location = CAMERA_FB_IN_DRAM;
        config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
    }

    const unsigned long startTime = millis();
    esp_err_t result = esp_camera_init(&config);
    const unsigned long duration = millis() - startTime;

    if (result != ESP_OK) {
        Serial.printf(
            "[CAMERA ERROR] Initialisation failed: 0x%x\n",
            result
        );
        return false;
    }

    Serial.printf(
        "[CAMERA] Initialised successfully in %lu ms\n",
        duration
    );

    sensor_t* sensor = esp_camera_sensor_get();

    if (sensor == NULL) {
        Serial.println("[CAMERA ERROR] Sensor handle is null.");
        esp_camera_deinit();
        return false;
    }

    // EDITH image configuration.
    sensor->set_framesize(sensor, FRAMESIZE_VGA);
    sensor->set_quality(sensor, 10);

    // Rotate image by 180 degrees.
    sensor->set_vflip(sensor, 1);
    sensor->set_hmirror(sensor, 1);

    // Neutral image adjustments.
    sensor->set_brightness(sensor, 0);
    sensor->set_contrast(sensor, 0);
    sensor->set_saturation(sensor, 0);
    sensor->set_sharpness(sensor, 0);

    // Automatic camera controls.
    sensor->set_exposure_ctrl(sensor, 1);
    sensor->set_gain_ctrl(sensor, 1);
    sensor->set_whitebal(sensor, 1);
    sensor->set_awb_gain(sensor, 1);

    Serial.println();
    Serial.println("========== EDITH CAMERA SENSOR ==========");
    Serial.printf("PID:  0x%04X\n", sensor->id.PID);
    Serial.printf("VER:  0x%04X\n", sensor->id.VER);
    Serial.printf("MIDH: 0x%02X\n", sensor->id.MIDH);
    Serial.printf("MIDL: 0x%02X\n", sensor->id.MIDL);
    Serial.printf("PSRAM: %s\n", hasPSRAM ? "YES" : "NO");
    Serial.println("Resolution: VGA (640x480)");
    Serial.println("Format: JPEG");
    Serial.println("Rotation: 180 degrees");
    Serial.println("=========================================");

    return true;
}

// ============================================================
// STILL IMAGE CAPTURE HANDLER
// GET /capture ON PORT 80
// ============================================================

static esp_err_t captureHandler(httpd_req_t* request) {
    const unsigned long captureStart = millis();

    camera_fb_t* frame = esp_camera_fb_get();

    const unsigned long captureDuration =
        millis() - captureStart;

    if (frame == NULL) {
        Serial.println("[CAPTURE ERROR] Frame capture failed.");

        httpd_resp_send_err(
            request,
            HTTPD_500_INTERNAL_SERVER_ERROR,
            "Camera capture failed"
        );

        return ESP_FAIL;
    }

    httpd_resp_set_type(request, "image/jpeg");
    httpd_resp_set_hdr(
        request,
        "Content-Disposition",
        "inline; filename=edith.jpg"
    );
    httpd_resp_set_hdr(request, "Cache-Control", "no-store");
    httpd_resp_set_hdr(request, "Access-Control-Allow-Origin", "*");

    esp_err_t result = httpd_resp_send(
        request,
        reinterpret_cast<const char*>(frame->buf),
        frame->len
    );

    const size_t imageSize = frame->len;
    const uint16_t width = frame->width;
    const uint16_t height = frame->height;

    // Always return the framebuffer, including on HTTP errors.
    esp_camera_fb_return(frame);

    if (result == ESP_OK) {
        Serial.printf(
            "[CAPTURE] OK | %ux%u | %u bytes | %lu ms\n",
            width,
            height,
            static_cast<unsigned int>(imageSize),
            captureDuration
        );
    } else {
        Serial.printf("[CAPTURE ERROR] HTTP result: 0x%x\n", result);
    }

    return result;
}

// ============================================================
// LIVE MJPEG STREAM HANDLER
// GET /stream ON PORT 81
// ============================================================

static esp_err_t streamHandler(httpd_req_t* request) {
    esp_err_t result = httpd_resp_set_type(
        request,
        STREAM_CONTENT_TYPE
    );

    if (result != ESP_OK) {
        return result;
    }

    httpd_resp_set_hdr(
        request,
        "Access-Control-Allow-Origin",
        "*"
    );
    httpd_resp_set_hdr(
        request,
        "Cache-Control",
        "no-store"
    );
    httpd_resp_set_hdr(
        request,
        "Connection",
        "close"
    );

    Serial.println("[STREAM] Client connected.");

    char partHeader[64];
    unsigned long frameCount = 0;
    unsigned long lastLog = millis();

    while (true) {
        camera_fb_t* frame = esp_camera_fb_get();

        if (frame == NULL) {
            Serial.println("[STREAM ERROR] Frame capture failed.");
            result = ESP_FAIL;
            break;
        }

        // Send multipart boundary.
        result = httpd_resp_send_chunk(
            request,
            STREAM_BOUNDARY,
            strlen(STREAM_BOUNDARY)
        );

        if (result != ESP_OK) {
            esp_camera_fb_return(frame);
            break;
        }

        // Send this frame's JPEG headers.
        const size_t headerLength = snprintf(
            partHeader,
            sizeof(partHeader),
            STREAM_PART_HEADER,
            static_cast<unsigned int>(frame->len)
        );

        result = httpd_resp_send_chunk(
            request,
            partHeader,
            headerLength
        );

        if (result != ESP_OK) {
            esp_camera_fb_return(frame);
            break;
        }

        // Send JPEG bytes.
        result = httpd_resp_send_chunk(
            request,
            reinterpret_cast<const char*>(frame->buf),
            frame->len
        );

        const size_t frameSize = frame->len;

        // Release frame memory before requesting the next frame.
        esp_camera_fb_return(frame);

        if (result != ESP_OK) {
            break;
        }

        frameCount++;

        if (millis() - lastLog >= 10000) {
            Serial.printf(
                "[STREAM] Sent %lu frames in the last 10 seconds.\n",
                frameCount
            );
            frameCount = 0;
            lastLog = millis();
        }

        (void)frameSize;
        taskYIELD();
    }

    Serial.println("[STREAM] Client disconnected.");
    return result;
}

// ============================================================
// START CAPTURE SERVER - PORT 80
// ============================================================

bool startCaptureServer() {
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();

    config.server_port = 80;
    config.ctrl_port = 32768;
    config.max_uri_handlers = 4;

    httpd_uri_t captureURI = {};
    captureURI.uri = "/capture";
    captureURI.method = HTTP_GET;
    captureURI.handler = captureHandler;
    captureURI.user_ctx = NULL;

    esp_err_t result = httpd_start(
        &captureServer,
        &config
    );

    if (result != ESP_OK) {
        Serial.printf(
            "[HTTP ERROR] Capture server failed: 0x%x\n",
            result
        );
        captureServer = NULL;
        return false;
    }

    result = httpd_register_uri_handler(
        captureServer,
        &captureURI
    );

    if (result != ESP_OK) {
        Serial.printf(
            "[HTTP ERROR] Could not register /capture: 0x%x\n",
            result
        );

        httpd_stop(captureServer);
        captureServer = NULL;
        return false;
    }

    Serial.println("[HTTP] Capture server ready on port 80.");
    Serial.println("[HTTP] Endpoint: /capture");

    return true;
}

// ============================================================
// START STREAM SERVER - PORT 81
// ============================================================

bool startStreamServer() {
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();

    config.server_port = 81;
    config.ctrl_port = 32769;
    config.max_uri_handlers = 4;
    config.stack_size = 8192;

    httpd_uri_t streamURI = {};
    streamURI.uri = "/stream";
    streamURI.method = HTTP_GET;
    streamURI.handler = streamHandler;
    streamURI.user_ctx = NULL;

    esp_err_t result = httpd_start(
        &streamServer,
        &config
    );

    if (result != ESP_OK) {
        Serial.printf(
            "[HTTP ERROR] Stream server failed: 0x%x\n",
            result
        );
        streamServer = NULL;
        return false;
    }

    result = httpd_register_uri_handler(
        streamServer,
        &streamURI
    );

    if (result != ESP_OK) {
        Serial.printf(
            "[HTTP ERROR] Could not register /stream: 0x%x\n",
            result
        );

        httpd_stop(streamServer);
        streamServer = NULL;
        return false;
    }

    Serial.println("[HTTP] Stream server ready on port 81.");
    Serial.println("[HTTP] Endpoint: /stream");

    return true;
}

// ============================================================
// WIFI CONNECTION
// ============================================================

void connectWiFi() {
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.setAutoReconnect(true);

    Serial.println();
    Serial.printf("[WIFI] Connecting to: %s\n", WIFI_SSID);

    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    int attempts = 0;

    while (WiFi.status() != WL_CONNECTED && attempts < 30) {
        delay(500);
        Serial.print(".");
        attempts++;
    }

    if (WiFi.status() == WL_CONNECTED) {
        wifiWasConnected = true;

        Serial.println();
        Serial.println("[WIFI] Connected!");
        Serial.print("[WIFI] IP address: ");
        Serial.println(WiFi.localIP());

        Serial.print("[WIFI] RSSI: ");
        Serial.print(WiFi.RSSI());
        Serial.println(" dBm");
    } else {
        wifiWasConnected = false;

        Serial.println();
        Serial.println("[WIFI WARNING] Initial connection failed.");
        Serial.println("[WIFI] Background reconnection remains enabled.");
    }

    lastWifiAttempt = millis();
}

// ============================================================
// WIFI CONNECTION MONITOR / RECOVERY
// ============================================================

void maintainWiFi() {
    const bool connected = (WiFi.status() == WL_CONNECTED);

    if (connected && !wifiWasConnected) {
        wifiWasConnected = true;

        Serial.println();
        Serial.println("[WIFI] Connection restored.");
        Serial.print("[WIFI] IP address: ");
        Serial.println(WiFi.localIP());
    }

    if (!connected && wifiWasConnected) {
        wifiWasConnected = false;
        Serial.println();
        Serial.println("[WIFI WARNING] Connection lost.");
    }

    if (
        !connected &&
        millis() - lastWifiAttempt >= WIFI_RETRY_INTERVAL
    ) {
        Serial.println("[WIFI] Attempting reconnection...");
        WiFi.reconnect();
        lastWifiAttempt = millis();
    }
}

// ============================================================
// SETUP
// ============================================================

void setup() {
    Serial.begin(115200);
    delay(1000);

    Serial.println();
    Serial.println("==========================================");
    Serial.println("           EDITH VISION SYSTEM");
    Serial.println("              CAMERA V1.3");
    Serial.println("==========================================");

    // Camera startup with up to three attempts.
    bool cameraReady = false;

    for (int attempt = 1; attempt <= 3; attempt++) {
        Serial.printf(
            "\n[CAMERA] Initialisation attempt %d/3\n",
            attempt
        );

        if (startCamera()) {
            cameraReady = true;
            break;
        }

        Serial.println("[CAMERA] Initialisation failed.");

        if (attempt < 3) {
            Serial.println("[CAMERA] Retrying in 2 seconds...");
            delay(2000);
        }
    }

    if (!cameraReady) {
        Serial.println("[FATAL] Camera could not initialise.");
        Serial.println("[SYSTEM] Restarting in 5 seconds...");
        delay(5000);
        ESP.restart();
    }

    // Connect to Wi-Fi.
    connectWiFi();

    // Start still capture endpoint.
    if (!startCaptureServer()) {
        Serial.println("[FATAL] Capture HTTP server failed.");
        delay(5000);
        ESP.restart();
    }

    // Start MJPEG live stream endpoint.
    if (!startStreamServer()) {
        Serial.println("[FATAL] Stream HTTP server failed.");
        delay(5000);
        ESP.restart();
    }

    Serial.println();
    Serial.println("==========================================");
    Serial.println("             EDITH IS ONLINE");
    Serial.println("==========================================");
    Serial.println("[SYSTEM] Camera: READY");
    Serial.println("[SYSTEM] Wi-Fi: see IP address above");
    Serial.println("[SYSTEM] Capture: http://<IP>/capture");
    Serial.println("[SYSTEM] Stream:  http://<IP>:81/stream");
    Serial.println("==========================================");
}

// ============================================================
// MAIN LOOP
// ============================================================

void loop() {
    maintainWiFi();
    delay(100);
}
