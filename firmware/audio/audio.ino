
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include "ESP_I2S.h"

// ==========================================
// EDITH - WIRELESS AUDIO TRANSMITTER
// ESP32 DevKit V1 + INMP441
// ==========================================

// YOUR WIFI DETAILS
const char* WIFI_SSID = "Tuhin";
const char* WIFI_PASSWORD = "$019730$";

// MACBOOK DETAILS
const char* MAC_IP = "10.0.100.51";
const uint16_t UDP_PORT = 5005;

// I2S MICROPHONE PINS
#define I2S_BCLK 26
#define I2S_WS   25
#define I2S_DIN  33

// AUDIO SETTINGS
#define SAMPLE_RATE 16000
#define BUFFER_SIZE 256

I2SClass I2S;
WiFiUDP udp;

int32_t rawSamples[BUFFER_SIZE];
int16_t audioSamples[BUFFER_SIZE];

void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println();
  Serial.println("================================");
  Serial.println(" EDITH WIRELESS AUDIO SYSTEM");
  Serial.println("================================");

  // CONNECT TO WIFI
  Serial.println("[EDITH] Connecting to WiFi...");

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }

  Serial.println();
  Serial.println("[EDITH] WiFi connected!");
  Serial.print("[EDITH] ESP32 IP: ");
  Serial.println(WiFi.localIP());

  // CONFIGURE MICROPHONE
  I2S.setPins(
    I2S_BCLK,
    I2S_WS,
    -1,
    I2S_DIN,
    -1
  );

  if (!I2S.begin(
        I2S_MODE_STD,
        SAMPLE_RATE,
        I2S_DATA_BIT_WIDTH_32BIT,
        I2S_SLOT_MODE_MONO
      )) {
    Serial.println("[EDITH] ERROR: Microphone initialization failed!");
    while (true) {
      delay(1000);
    }
  }

  Serial.println("[EDITH] Microphone initialized!");

  // START UDP
  if (!udp.begin(UDP_PORT)) {
    Serial.println("[EDITH] ERROR: UDP initialization failed!");
    while (true) {
      delay(1000);
    }
  }

  Serial.println("[EDITH] UDP transmitter ready!");
  Serial.println("[EDITH] Streaming microphone audio...");
}

void loop() {
  // READ MICROPHONE SAMPLES
  for (int i = 0; i < BUFFER_SIZE; i++) {
    rawSamples[i] = I2S.read();

    // Convert 32-bit I2S sample to 16-bit PCM
    audioSamples[i] = (int16_t)(rawSamples[i] >> 16);
  }

  // SEND AUDIO TO MACBOOK
  udp.beginPacket(MAC_IP, UDP_PORT);

  udp.write(
    (uint8_t*)audioSamples,
    sizeof(audioSamples)
  );

  udp.endPacket();
}
