
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include "ESP_I2S.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

// ==========================================
// EDITH - FINAL COMBINED WIRELESS AUDIO
// ESP32 DevKit V1
// Arduino-ESP32 4.0.0
//
// I2S 0: INMP441 microphone
// I2S 1: Speaker output
// ==========================================

// WIFI DETAILS
const char* WIFI_SSID = "Tuhin";
const char* WIFI_PASSWORD = "$019730$";

// MACBOOK DETAILS
const char* MAC_IP = "10.0.100.51";

// UDP PORTS
const uint16_t MIC_PORT = 5005;
const uint16_t SPEAKER_PORT = 5006;

// ==========================================
// MICROPHONE - I2S 0
// ==========================================

#define MIC_BCLK 26
#define MIC_WS   25
#define MIC_DIN  33

#define MIC_SAMPLE_RATE 16000
#define MIC_BUFFER_SIZE 256

I2SClass I2S(I2S_NUM_0);
WiFiUDP micUdp;

int32_t rawSamples[MIC_BUFFER_SIZE];
int16_t audioSamples[MIC_BUFFER_SIZE];

// ==========================================
// SPEAKER - I2S 1
// ==========================================

#define SPEAKER_BCLK 27
#define SPEAKER_WS   14
#define SPEAKER_DOUT 22

#define SPEAKER_SAMPLE_RATE 16000
#define SPEAKER_BUFFER_SIZE 1024

I2SClass SpeakerI2S(I2S_NUM_1);
WiFiUDP speakerUdp;

uint8_t speakerBuffer[SPEAKER_BUFFER_SIZE];

// ==========================================
// NETWORK AND TASK CONTROL
// ==========================================

SemaphoreHandle_t udpMutex = nullptr;

volatile bool udpReady = false;

TaskHandle_t micTaskHandle = nullptr;
TaskHandle_t speakerTaskHandle = nullptr;

unsigned long micPacketCount = 0;
unsigned long micFailureCount = 0;
unsigned long speakerPacketCount = 0;
unsigned long speakerFailureCount = 0;

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
      speakerUdp.stop();

      udpReady = false;

      xSemaphoreGive(udpMutex);

      Serial.println("[EDITH NETWORK] UDP sockets closed.");
    }
  }

  else if (connected && !udpReady) {

    if (xSemaphoreTake(udpMutex, pdMS_TO_TICKS(100)) == pdTRUE) {

      micUdp.stop();
      speakerUdp.stop();

      bool micOk = micUdp.begin(MIC_PORT);
      bool speakerOk = speakerUdp.begin(SPEAKER_PORT);

      if (micOk && speakerOk) {

        udpReady = true;

        Serial.println("[EDITH NETWORK] UDP sockets ready.");
        Serial.println("[EDITH NETWORK] Microphone port: 5005");
        Serial.println("[EDITH NETWORK] Speaker port: 5006");

      } else {

        micUdp.stop();
        speakerUdp.stop();

        udpReady = false;

        Serial.println("[EDITH ERROR] UDP socket initialisation failed.");
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

    // Preserve the original working microphone pipeline.
    for (int i = 0; i < MIC_BUFFER_SIZE; i++) {

      rawSamples[i] = I2S.read();

      audioSamples[i] =
        (int16_t)(rawSamples[i] >> 16);
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
// SPEAKER TASK
// ==========================================

void speakerTask(void* parameter) {

  Serial.println("[EDITH SPEAKER] Task running.");

  while (true) {

    if (!udpReady || WiFi.status() != WL_CONNECTED) {

      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }

    if (xSemaphoreTake(udpMutex, pdMS_TO_TICKS(20)) != pdTRUE) {

      vTaskDelay(pdMS_TO_TICKS(1));
      continue;
    }

    int packetSize = speakerUdp.parsePacket();

    if (packetSize <= 0) {

      xSemaphoreGive(udpMutex);

      vTaskDelay(pdMS_TO_TICKS(1));
      continue;
    }

    // Read the packet without exceeding the buffer.
    int bytesToRead = packetSize;

    if (bytesToRead > (int)sizeof(speakerBuffer)) {
      bytesToRead = sizeof(speakerBuffer);
    }

    int received = speakerUdp.read(
      speakerBuffer,
      bytesToRead
    );

    // Discard any remaining oversized packet bytes.
    while (speakerUdp.available() > 0) {
      speakerUdp.read();
    }

    xSemaphoreGive(udpMutex);

    if (received <= 0) {

      speakerFailureCount++;
      continue;
    }

    // 16-bit PCM requires an even byte count.
    if (received % 2 != 0) {
      received--;
    }

    if (received <= 0) {
      speakerFailureCount++;
      continue;
    }

    size_t written = SpeakerI2S.write(
      speakerBuffer,
      received
    );

    if (written == (size_t)received) {

      speakerPacketCount++;

    } else {

      speakerFailureCount++;
    }

    // Avoid excessive Serial output.
    if ((speakerPacketCount + speakerFailureCount) % 100 == 0) {

      Serial.print("[EDITH SPEAKER] Received: ");
      Serial.print(speakerPacketCount);

      Serial.print(" | Write failures: ");
      Serial.println(speakerFailureCount);
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
  Serial.println("       EDITH DEVKIT AUDIO SYSTEM");
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
    Serial.println("[EDITH] Audio hardware will still initialise.");
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
        I2S_SLOT_MODE_MONO
      )) {

    Serial.println("[FATAL] Microphone I2S initialisation failed!");

    while (true) {
      delay(1000);
    }
  }

  Serial.println("[EDITH] Microphone I2S 0 ready.");

  // ========================================
  // SPEAKER INITIALISATION - I2S 1
  // ========================================

  Serial.println("[EDITH] Initialising speaker I2S...");

  SpeakerI2S.setPins(
    SPEAKER_BCLK,
    SPEAKER_WS,
    SPEAKER_DOUT,
    -1
  );

  // Send each mono sample to BOTH I2S slots. MAX98357A breakout
  // boards can be configured for left, right, or mixed input; using
  // BOTH avoids silence when the amplifier is listening to the other slot.
  if (!SpeakerI2S.begin(
        I2S_MODE_STD,
        SPEAKER_SAMPLE_RATE,
        I2S_DATA_BIT_WIDTH_16BIT,
        I2S_SLOT_MODE_MONO,
        I2S_STD_SLOT_BOTH
      )) {

    Serial.println("[FATAL] Speaker I2S initialisation failed!");

    while (true) {
      delay(1000);
    }
  }

  Serial.println("[EDITH] Speaker I2S 1 ready.");

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
  // START SPEAKER TASK
  // ========================================

  Serial.println("[EDITH] Starting speaker task...");

  BaseType_t speakerResult = xTaskCreate(
    speakerTask,
    "EDITH_Speaker",
    4096,
    nullptr,
    3,
    &speakerTaskHandle
  );

  if (speakerResult != pdPASS) {

    Serial.println("[FATAL] Speaker task creation failed!");

    vTaskDelete(micTaskHandle);
    micTaskHandle = nullptr;

    while (true) {
      delay(1000);
    }
  }

  // ========================================
  // FINAL STATUS
  // ========================================

  Serial.println();
  Serial.println("========================================");
  Serial.println("       EDITH AUDIO SYSTEM READY");
  Serial.println("========================================");

  Serial.println("[EDITH] Microphone I2S: READY");
  Serial.println("[EDITH] Speaker I2S: READY");

  Serial.println("[EDITH] Microphone task: RUNNING");
  Serial.println("[EDITH] Speaker task: RUNNING");

  Serial.println("[EDITH] Microphone UDP: PORT 5005");
  Serial.println("[EDITH] Speaker UDP: PORT 5006");

  Serial.println("[EDITH] Wi-Fi recovery: ENABLED");

  Serial.println("[EDITH] Physical speaker playback:");
  Serial.println("[EDITH] NOT YET VERIFIED - amplifier pending");

  Serial.println("========================================");
}

// ==========================================
// MAIN LOOP
// ==========================================

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
