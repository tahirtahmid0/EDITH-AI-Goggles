
#include "esp_camera.h"
#include <WiFi.h>
#include "esp_http_server.h"

// ==========================================
// EDITH - CAMERA FIRMWARE V1.1
// Board: AI Thinker ESP32-CAM
// ==========================================

// Enter your Wi-Fi credentials locally.
const char* WIFI_SSID = "Tuhin";
const char* WIFI_PASSWORD = "$019730$";

// ==========================================
// AI THINKER ESP32-CAM PIN CONFIGURATION
// ==========================================

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

// ==========================================
// GLOBAL VARIABLES
// ==========================================

httpd_handle_t cameraServer = NULL;

bool wifiWasConnected = false;

unsigned long lastWifiAttempt = 0;

const unsigned long WIFI_RETRY_INTERVAL = 10000;

// ==========================================
// CAMERA INITIALIZATION
// ==========================================

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

    config.pin_sccb_sda = SIOD_GPIO_NUM;
    config.pin_sccb_scl = SIOC_GPIO_NUM;

    config.pin_pwdn = PWDN_GPIO_NUM;
    config.pin_reset = RESET_GPIO_NUM;

    config.xclk_freq_hz = 20000000;

    // ======================================
    // MEMORY CONFIGURATION
    // ======================================

    bool hasPSRAM = psramFound();

    if (hasPSRAM) {

        Serial.println("[CAMERA] PSRAM detected!");

        Serial.printf(
            "[CAMERA] PSRAM size: %u bytes\n",
            ESP.getPsramSize()
        );

        config.fb_count = 2;
        config.fb_location = CAMERA_FB_IN_PSRAM;
        config.grab_mode = CAMERA_GRAB_LATEST;

    } else {

        Serial.println("[WARNING] PSRAM NOT DETECTED!");

        config.fb_count = 1;
        config.fb_location = CAMERA_FB_IN_DRAM;
        config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
    }

    // ======================================
    // IMAGE CONFIGURATION
    // ======================================

    config.frame_size = FRAMESIZE_VGA;
    config.pixel_format = PIXFORMAT_JPEG;
    config.jpeg_quality = 10;

    // ======================================
    // INITIALIZE CAMERA
    // ======================================

    unsigned long initStart = millis();

    esp_err_t result = esp_camera_init(&config);

    unsigned long initDuration = millis() - initStart;

    if (result != ESP_OK) {

        Serial.printf(
            "[CAMERA ERROR] Initialization failed: 0x%x\n",
            result
        );

        return false;
    }

    Serial.printf(
        "[CAMERA] Initialization completed in %lu ms\n",
        initDuration
    );

    // ======================================
    // SENSOR CONFIGURATION
    // ======================================

    sensor_t* s = esp_camera_sensor_get();

    if (s == NULL) {

        Serial.println(
            "[CAMERA ERROR] Could not access camera sensor!"
        );

        esp_camera_deinit();

        return false;
    }

    // Preserve EDITH's existing image settings.

    s->set_framesize(s, FRAMESIZE_VGA);
    s->set_quality(s, 10);

    // Keep the image vertically flipped for the existing mounting
    // orientation, but disable horizontal mirroring so text reads normally.

    s->set_vflip(s, 1);
    s->set_hmirror(s, 0);

    Serial.println(
        "[CAMERA] Vertical flip enabled; horizontal mirroring disabled."
    );

    // Image adjustments.

    s->set_brightness(s, 0);
    s->set_contrast(s, 0);
    s->set_saturation(s, 0);
    s->set_sharpness(s, 0);

    // Automatic image controls.

    s->set_exposure_ctrl(s, 1);
    s->set_gain_ctrl(s, 1);
    s->set_whitebal(s, 1);
    s->set_awb_gain(s, 1);

    // Sensor information.

    Serial.println();
    Serial.println("========== CAMERA SENSOR INFO ==========");

    Serial.printf("PID: 0x%04X\n", s->id.PID);
    Serial.printf("VER: 0x%04X\n", s->id.VER);
    Serial.printf("MIDH: 0x%02X\n", s->id.MIDH);
    Serial.printf("MIDL: 0x%02X\n", s->id.MIDL);

    Serial.println("========================================");

    return true;
}

// ==========================================
// CAMERA CAPTURE ENDPOINT
// GET /capture
// ==========================================


static esp_err_t captureHandler(httpd_req_t* request) {

    // ======================================
    // CAMERA CAPTURE
    // ======================================

    unsigned long captureStart = millis();

    camera_fb_t* frame = esp_camera_fb_get();

    unsigned long captureDuration =
        millis() - captureStart;

    if (frame == NULL) {

        Serial.println(
            "[CAMERA ERROR] Image capture failed!"
        );

        httpd_resp_send_err(
            request,
            HTTPD_500_INTERNAL_SERVER_ERROR,
            "Camera capture failed"
        );

        return ESP_FAIL;
    }

    size_t imageSize = frame->len;

    uint16_t imageWidth = frame->width;
    uint16_t imageHeight = frame->height;

    // ======================================
    // HTTP RESPONSE
    // ======================================

    unsigned long transmissionStart = millis();

    esp_err_t result = httpd_resp_set_type(
        request,
        "image/jpeg"
    );

    if (result == ESP_OK) {

        result = httpd_resp_set_hdr(
            request,
            "Content-Disposition",
            "inline; filename=edith.jpg"
        );
    }

    if (result == ESP_OK) {

        result = httpd_resp_set_hdr(
            request,
            "Cache-Control",
            "no-store"
        );
    }

    if (result == ESP_OK) {

        result = httpd_resp_send(
            request,
            (const char*)frame->buf,
            frame->len
        );
    }

    unsigned long transmissionDuration =
        millis() - transmissionStart;

    // ======================================
    // RELEASE FRAMEBUFFER
    // ======================================

    esp_camera_fb_return(frame);

    unsigned long totalDuration =
        captureDuration + transmissionDuration;

    // ======================================
    // DIAGNOSTICS
    // ======================================

    if (result == ESP_OK) {

        Serial.printf(
            "[CAMERA] SUCCESS | %ux%u | %u bytes\n",
            imageWidth,
            imageHeight,
            (unsigned int)imageSize
        );

        Serial.printf(
            "[TIMING] Capture: %lu ms | "
            "Transmission: %lu ms | "
            "Total: %lu ms\n",
            captureDuration,
            transmissionDuration,
            totalDuration
        );

    } else {

        Serial.printf(
            "[HTTP ERROR] Transmission failed: "
            "0x%x\n",
            result
        );

        Serial.printf(
            "[TIMING] Capture: %lu ms | "
            "Failed transmission: %lu ms\n",
            captureDuration,
            transmissionDuration
        );

        Serial.printf(
            "[HTTP ERROR] Image size: %u bytes\n",
            (unsigned int)imageSize
        );
    }

    return result;
}


// ==========================================
// START HTTP SERVER
// ==========================================

bool startWebServer() {

    httpd_config_t serverConfig =
        HTTPD_DEFAULT_CONFIG();

    serverConfig.server_port = 80;

    httpd_uri_t captureURI = {};

    captureURI.uri = "/capture";
    captureURI.method = HTTP_GET;
    captureURI.handler = captureHandler;
    captureURI.user_ctx = NULL;

    // Start server.

    esp_err_t result = httpd_start(
        &cameraServer,
        &serverConfig
    );

    if (result != ESP_OK) {

        Serial.printf(
            "[HTTP ERROR] Server startup failed: 0x%x\n",
            result
        );

        cameraServer = NULL;

        return false;
    }

    // Register capture endpoint.

    result = httpd_register_uri_handler(
        cameraServer,
        &captureURI
    );

    if (result != ESP_OK) {

        Serial.printf(
            "[HTTP ERROR] Endpoint registration failed: 0x%x\n",
            result
        );

        httpd_stop(cameraServer);

        cameraServer = NULL;

        return false;
    }

    Serial.println("[HTTP] Camera server started!");
    Serial.println("[HTTP] Endpoint: /capture");
    Serial.println("[HTTP] Port: 80");

    return true;
}

// ==========================================
// WIFI INITIALIZATION
// ==========================================

void connectWiFi() {

    WiFi.mode(WIFI_STA);

    WiFi.setSleep(false);
    WiFi.setAutoReconnect(true);

    Serial.println();
    Serial.println("[WIFI] Connecting...");

    WiFi.begin(
        WIFI_SSID,
        WIFI_PASSWORD
    );

    int attempts = 0;

    while (
        WiFi.status() != WL_CONNECTED &&
        attempts < 30
    ) {

        delay(500);

        Serial.print(".");

        attempts++;
    }

    if (WiFi.status() == WL_CONNECTED) {

        wifiWasConnected = true;

        Serial.println();
        Serial.println("[WIFI] Connected!");

        Serial.print("[WIFI] IP Address: ");
        Serial.println(WiFi.localIP());

        Serial.print("[WIFI] Signal strength: ");
        Serial.print(WiFi.RSSI());
        Serial.println(" dBm");

    } else {

        wifiWasConnected = false;

        Serial.println();
        Serial.println(
            "[WIFI WARNING] Initial connection failed!"
        );

        Serial.println(
            "[WIFI] Automatic reconnection will continue."
        );
    }

    lastWifiAttempt = millis();
}

// ==========================================
// WIFI RECOVERY
// ==========================================

void maintainWiFi() {

    bool connected = (
        WiFi.status() == WL_CONNECTED
    );

    // Detect successful reconnection.

    if (connected && !wifiWasConnected) {

        wifiWasConnected = true;

        Serial.println();
        Serial.println("[WIFI] Connection restored!");

        Serial.print("[WIFI] IP Address: ");
        Serial.println(WiFi.localIP());
    }

    // Detect disconnection.

    if (!connected && wifiWasConnected) {

        wifiWasConnected = false;

        Serial.println();
        Serial.println(
            "[WIFI WARNING] Connection lost!"
        );
    }

    // Retry periodically.

    if (
        !connected &&
        millis() - lastWifiAttempt >= WIFI_RETRY_INTERVAL
    ) {

        Serial.println(
            "[WIFI] Attempting reconnection..."
        );

        WiFi.reconnect();

        lastWifiAttempt = millis();
    }
}

// ==========================================
// SETUP
// ==========================================

void setup() {

    Serial.begin(115200);

    delay(1000);

    Serial.println();
    Serial.println("================================");
    Serial.println("       EDITH VISION SYSTEM");
    Serial.println("           VERSION 1.1");
    Serial.println("================================");

    // ======================================
    // CAMERA STARTUP WITH RETRIES
    // ======================================

    bool cameraReady = false;

    for (int attempt = 1; attempt <= 3; attempt++) {

        Serial.printf(
            "\n[CAMERA] Initialization attempt %d/3\n",
            attempt
        );

        if (startCamera()) {

            cameraReady = true;

            Serial.println(
                "[CAMERA] Initialization successful!"
            );

            break;
        }

        Serial.println(
            "[CAMERA] Initialization failed."
        );

        if (attempt < 3) {

            Serial.println(
                "[CAMERA] Retrying in 2 seconds..."
            );

            delay(2000);
        }
    }

    if (!cameraReady) {

        Serial.println();
        Serial.println(
            "[FATAL] Camera initialization failed!"
        );

        Serial.println(
            "[SYSTEM] Restarting in 5 seconds..."
        );

        delay(5000);

        ESP.restart();
    }

    // ======================================
    // WIFI STARTUP
    // ======================================

    connectWiFi();

    // ======================================
    // HTTP SERVER STARTUP
    // ======================================

    if (!startWebServer()) {

        Serial.println(
            "[FATAL] Could not start camera server!"
        );

        Serial.println(
            "[SYSTEM] Restarting in 5 seconds..."
        );

        delay(5000);

        ESP.restart();
    }

    // ======================================
    // SYSTEM READY
    // ======================================

    Serial.println();
    Serial.println("================================");
    Serial.println("       EDITH IS ONLINE!");
    Serial.println("================================");

    Serial.println("[SYSTEM] Camera: READY");
    Serial.println("[SYSTEM] HTTP server: READY");
    Serial.println("[SYSTEM] Capture: /capture");

    Serial.println("================================");
}

// ==========================================
// MAIN LOOP
// ==========================================

void loop() {

    maintainWiFi();

    delay(100);
}
