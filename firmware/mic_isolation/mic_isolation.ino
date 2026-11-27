
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include "ESP_I2S.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

// ==========================================
// EDITH - WIRELESS MICROPHONE FIRMWARE
// ESP32 DevKit V1
// Arduino-ESP32 4.0.0
//
// I2S 0: INMP441 microphone
// UDP: microphone audio to MacBook
// ==========================================

// WIFI DETAILS
const char* WIFI_SSID = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// MACBOOK DETAILS
const char* MAC_IP = "172.20.10.4";

// MICROPHONE UDP PORT
const uint16_t MIC_PORT = 5005;

// ==========================================
// MICROPHONE - I2S 0
// ==========================================

#define MIC_BCLK 26
#define MIC_WS   25
#define MIC_DIN  33

#define MIC_SAMPLE_RATE 16000
#define MIC_BUFFER_SIZE 256
#define I2S_SLOT_SAMPLE_COUNT (MIC_BUFFER_SIZE * 2)

I2SClass I2S(I2S_NUM_0);
WiFiUDP micUdp;

int32_t rawSamples[I2S_SLOT_SAMPLE_COUNT];
int16_t audioSamples[MIC_BUFFER_SIZE];

// ==========================================
// NETWORK AND TASK CONTROL
// ==========================================

SemaphoreHandle_t udpMutex = nullptr;

volatile bool udpReady = false;

TaskHandle_t micTaskHandle = nullptr;

unsigned long micPacketCount = 0;
unsigned long micFailureCount = 0;

unsigned long lastReconnectAttempt = 0;

bool previouslyConnected = false;

// ==========================================
// UDP SOCKET MANAGEMENT
// Called ONLY from setup()/loop()
// ==========================================

void maintainUdpSockets() {

  bool connected = (WiFi.status() == WL_CONNECTED);

  if (udpMutex == nullptr) {
    return;
  }

  if (!connected && udpReady) {

    if (xSemaphoreTake(udpMutex, pdMS_TO_TICKS(100)) == pdTRUE) {
      micUdp.stop();
      udpReady = false;
      xSemaphoreGive(udpMutex);
      Serial.println("[EDITH NETWORK] Microphone UDP socket closed.");
    }
  }

  else if (connected && !udpReady) {

    if (xSemaphoreTake(udpMutex, pdMS_TO_TICKS(100)) == pdTRUE) {
      micUdp.stop();

      if (micUdp.begin(MIC_PORT)) {
        udpReady = true;
        Serial.println("[EDITH NETWORK] Microphone UDP socket ready.");
        Serial.println("[EDITH NETWORK] Microphone port: 5005");
      } else {
        micUdp.stop();
        udpReady = false;
        Serial.println("[EDITH ERROR] Microphone UDP initialisation failed.");
      }

      xSemaphoreGive(udpMutex);
    }
  }
}

// ==========================================
// MICROPHONE TASK
// ==========================================

void microphoneTask(void* parameter) {

  Serial.println("[EDITH MIC] Task running.");

  while (true) {

    // Read complete 32-bit I2S samples. I2S.read() returns ONE BYTE,
    // not a 32-bit sample; using it here silently produced all-zero PCM.
    size_t bytesRead = I2S.readBytes(
      (char*)rawSamples,
      sizeof(rawSamples)
    );

    // Only transmit complete 256-sample / 512-byte PCM packets.
    if (bytesRead != sizeof(rawSamples)) {
      continue;
    }

    for (int i = 0; i < MIC_BUFFER_SIZE; i++) {
      // Stereo RX returns interleaved [LEFT, RIGHT] 32-bit slots.
      // INMP441 L/R is grounded, so its signal is in the LEFT slot.
      // Extract only that slot and keep its upper 16 bits as PCM.
      audioSamples[i] = (int16_t)(rawSamples[i * 2] >> 16);
    }

    // Continue reading even if Wi-Fi is disconnected.
    if (!udpReady || WiFi.status() != WL_CONNECTED) {
      continue;
    }

    if (xSemaphoreTake(udpMutex, pdMS_TO_TICKS(20)) != pdTRUE) {
      continue;
    }

    // Recheck after acquiring the mutex.
    if (!udpReady || WiFi.status() != WL_CONNECTED) {

      xSemaphoreGive(udpMutex);
      continue;
    }

    bool success = false;

    if (micUdp.beginPacket(MAC_IP, MIC_PORT)) {

      size_t bytesWritten = micUdp.write(
        (uint8_t*)audioSamples,
        sizeof(audioSamples)
      );

      if (bytesWritten == sizeof(audioSamples)) {
        success = (micUdp.endPacket() == 1);
      } else {
        micUdp.endPacket();
      }
    }

    if (success) {

      micPacketCount++;

    } else {

      micFailureCount++;
    }

    xSemaphoreGive(udpMutex);

    // Report every 500 packets, not continuously.
    if ((micPacketCount + micFailureCount) % 500 == 0) {

      Serial.print("[EDITH MIC] Sent: ");
      Serial.print(micPacketCount);

      Serial.print(" | Failed: ");
      Serial.println(micFailureCount);
    }
  }
}

// ==========================================
// SETUP
// ==========================================

void setup() {

  Serial.begin(115200);
  delay(1000);

  Serial.println();
  Serial.println("========================================");
  Serial.println("     EDITH MICROPHONE SYSTEM");
  Serial.println("========================================");

  // ========================================
  // CREATE UDP MUTEX
  // ========================================

  udpMutex = xSemaphoreCreateMutex();

  if (udpMutex == nullptr) {

    Serial.println("[FATAL] Could not create UDP mutex.");

    while (true) {
      delay(1000);
    }
  }

  Serial.println("[EDITH] UDP mutex created.");

  // ========================================
  // WIFI INITIALISATION
  // ========================================

  Serial.println("[EDITH] Connecting to Wi-Fi...");

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long wifiStart = millis();

  while (
    WiFi.status() != WL_CONNECTED &&
    millis() - wifiStart < 15000
  ) {

    delay(500);
    Serial.print(".");
  }

  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {

    Serial.println("[EDITH] Wi-Fi connected!");

    Serial.print("[EDITH] ESP32 IP: ");
    Serial.println(WiFi.localIP());

    previouslyConnected = true;

  } else {

    Serial.println("[EDITH WARNING] Wi-Fi not connected yet.");
    Serial.println("[EDITH] Microphone will initialise.");
    Serial.println("[EDITH] Background reconnection enabled.");

    previouslyConnected = false;
  }

  lastReconnectAttempt = millis();

  // ========================================
  // MICROPHONE INITIALISATION - I2S 0
  // ========================================

  Serial.println("[EDITH] Initialising INMP441...");

  I2S.setPins(
    MIC_BCLK,
    MIC_WS,
    -1,
    MIC_DIN,
    -1
  );

  if (!I2S.begin(
        I2S_MODE_STD,
        MIC_SAMPLE_RATE,
        I2S_DATA_BIT_WIDTH_32BIT,
        I2S_SLOT_MODE_STEREO
      )) {

    Serial.println("[FATAL] Microphone I2S initialisation failed!");

    while (true) {
      delay(1000);
    }
  }

  Serial.println("[EDITH] Microphone I2S 0 ready.");

  // ========================================
  // INITIALISE UDP IF WIFI IS AVAILABLE
  // ========================================

  maintainUdpSockets();

  // ========================================
  // START MICROPHONE TASK
  // ========================================

  Serial.println("[EDITH] Starting microphone task...");

  BaseType_t micResult = xTaskCreate(
    microphoneTask,
    "EDITH_Microphone",
    4096,
    nullptr,
    1,
    &micTaskHandle
  );

  if (micResult != pdPASS) {

    Serial.println("[FATAL] Microphone task creation failed!");

    while (true) {
      delay(1000);
    }
  }

    // ========================================
  // FINAL STATUS
// ========================================

  Serial.println();
  Serial.println("========================================");
  Serial.println("     EDITH MICROPHONE SYSTEM READY");
  Serial.println("========================================");
  Serial.println("[EDITH] Microphone I2S: READY");
  Serial.println("[EDITH] Microphone task: RUNNING");
  Serial.println("[EDITH] Microphone UDP: PORT 5005");
  Serial.println("[EDITH] Wi-Fi recovery: ENABLED");
  Serial.println("========================================");
}

void loop() {

  bool connected = (WiFi.status() == WL_CONNECTED);

  // Detect Wi-Fi disconnection.
  if (!connected && previouslyConnected) {

    Serial.println("[EDITH NETWORK] Wi-Fi connection lost.");

    previouslyConnected = false;
  }

  // Detect successful reconnection.
  if (connected && !previouslyConnected) {

    Serial.println("[EDITH NETWORK] Wi-Fi reconnected!");

    Serial.print("[EDITH NETWORK] IP: ");
    Serial.println(WiFi.localIP());

    previouslyConnected = true;
  }

  // Attempt reconnection periodically.
  if (
    !connected &&
    millis() - lastReconnectAttempt >= 10000
  ) {

    lastReconnectAttempt = millis();

    Serial.println("[EDITH NETWORK] Attempting Wi-Fi recovery...");

    WiFi.reconnect();
  }

  // Restore UDP sockets when the network is available.
  maintainUdpSockets();

  vTaskDelay(pdMS_TO_TICKS(100));
}
