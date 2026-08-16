//
// Nesso N1 construction-safety monitor
// EDGE PROCESSING + PERSISTENT FLASH BUFFERING + CLOUD SYNC
//
// What this version does:
// 1. Samples IMU locally at 100 Hz.
// 2. Runs FFH / STF / near-miss detection on the Nesso itself.
// 3. Keeps detecting even when Wi-Fi is unavailable.
// 4. Persists offline cloud samples to LittleFS flash.
// 5. Keeps a 3-second pre-event ring buffer.
// 6. Captures 5 seconds of high-resolution data after a local event.
// 7. Sends buffered data to Render when Wi-Fi returns.
// 8. Network upload runs in a separate FreeRTOS task so HTTP requests
//    do not stop the 100 Hz edge detector.
//
// IMPORTANT:
// - Thresholds below are prototype starting values.
// - Calibrate them using your labelled FFH / STF / near-miss trials.
// - Offline buffering uses LittleFS flash and survives reset/power interruption.
// - secrets.h contains Wi-Fi credentials and the Render ingest API key.
//   Do not upload secrets.h to a public GitHub repository.
//

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>

#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

#include <Arduino_Nesso_N1.h>
#include <Arduino_BMI270_BMM150.h>
#include <M5GFX.h>

#include <math.h>

#include "secrets.h"

#include <FS.h>
#include <LittleFS.h>
#include <Preferences.h>

M5GFX display;
NessoBattery battery;


// ============================================================
// IDENTITY
// ============================================================

#define BLE_SERVER_NAME "Yong_Pin_Nesso"

const char *WORKER_ID =
    "Yong_Pin";


// Keep your existing BLE UUIDs.
#define SERVICE_UUID "bdc766fc-7eee-417f-bbe0-2e71a8a2bf70"
#define ACCEL_UUID "cba1d466-344c-4be3-ab3f-189f80dd7518"
#define GYRO_UUID "19a36902-0338-413f-90e5-b429fcd37164"
#define ACCEL_GYRO_UUID "f509416c-3c4b-401e-a768-b25a9e621a91"

// Extra local safety-alert characteristic.
// This is optional for the cloud system, but useful if a BLE receiver is connected.
#define SAFETY_ALERT_UUID "7df47fb8-d55f-4d36-bb1a-66dc10f16b31"


// ============================================================
// SAMPLING
// ============================================================

const int SAMPLE_RATE_HZ = 100;

const unsigned long SAMPLE_INTERVAL_MS =
    1000UL / SAMPLE_RATE_HZ;

const unsigned long BATTERY_INTERVAL_MS = 10000;
const unsigned long DISPLAY_INTERVAL_MS = 200;


// ============================================================
// EDGE-DETECTION THRESHOLDS
// Prototype starting values aligned with the cloud detector.
// Tune using your own labelled datasets.
// ============================================================

const float FREE_FALL_THRESHOLD_G = 0.45f;
const unsigned long FREE_FALL_MIN_MS = 120;
const unsigned long IMPACT_WAIT_MS = 1500;

const float HARD_IMPACT_THRESHOLD_G = 2.50f;

// FFH has a lower gyro requirement because the free-fall phase
// is already strong evidence.
const float FFH_ROTATION_DPS = 120.0f;

// STF has no confirmed free-fall phase, so require stronger rotation.
const float STF_ROTATION_DPS = 180.0f;

// Near-miss starting point.
const float NEAR_MISS_ROTATION_DPS = 220.0f;
const float NEAR_MISS_HIGH_ACCEL_G = 1.80f;
const float NEAR_MISS_LOW_ACCEL_G = 0.60f;
const unsigned long NEAR_MISS_MIN_MS = 120;

// Post-impact analysis.
const float STATIONARY_ACCEL_TOLERANCE_G = 0.22f;
const float STATIONARY_GYRO_DPS = 25.0f;
const unsigned long POST_IMPACT_ANALYSIS_MS = 1800;
const unsigned long POST_IMPACT_MAX_MS = 3000;
const unsigned long REQUIRED_STATIONARY_MS = 650;

const float FALL_ORIENTATION_CHANGE_DEG = 20.0f;

// Avoid repeated alerts for the same movement.
const unsigned long NEAR_MISS_COOLDOWN_MS = 12000;
const unsigned long CRITICAL_ALERT_COOLDOWN_MS = 12000;


// ============================================================
// CLOUD / OFFLINE BUFFER SETTINGS
// ============================================================

// Normal online data: approximately 20 Hz.
const int ONLINE_NORMAL_DIVIDER = 5;

// Normal offline data: approximately 1 Hz.
// Important motion is still kept immediately.
const int OFFLINE_NORMAL_DIVIDER = 100;

// One HTTP request sends at most 20 samples.
const int CLOUD_BATCH_SIZE = 20;

// RAM queue. At 1 Hz normal offline storage, 1200 slots is about
// 20 minutes if no high-resolution event occurs.
// We reserve most free space for a possible fall replay.
const int CLOUD_QUEUE_CAPACITY = 1200;
const int EVENT_RESERVED_SLOTS = 850;

// Keep 3 seconds at the full 100 Hz locally.
const int PRE_EVENT_SECONDS = 3;
const int PRE_EVENT_SAMPLE_COUNT =
    SAMPLE_RATE_HZ * PRE_EVENT_SECONDS;

// Capture every sample for 5 seconds after a local event.
const unsigned long POST_EVENT_HIGH_RES_MS = 5000;

// Network retry timing.
const unsigned long WIFI_RETRY_MS = 10000;
const unsigned long HTTP_RETRY_MS = 3000;
const unsigned long HTTP_FLUSH_MS = 1000;


// ============================================================
// SAMPLE STRUCTURE
// ============================================================

struct CloudSample {
    float t;
    float ax;
    float ay;
    float az;
    float gx;
    float gy;
    float gz;
};


// ============================================================
// FREE RTOS CLOUD QUEUE
// ============================================================

QueueHandle_t cloudQueue = nullptr;
TaskHandle_t networkTaskHandle = nullptr;

volatile unsigned long droppedNormalSamples = 0;
volatile unsigned long droppedImportantSamples = 0;
volatile unsigned long successfulCloudBatches = 0;
volatile unsigned long failedCloudBatches = 0;


// ============================================================
// PERSISTENT FLASH BUFFER
// ============================================================
const char *OFFLINE_BUFFER_FILE =
    "/nesso_offline.bin";

const char *NVS_NAMESPACE =
    "nessoBuf";

const char *NVS_CURSOR_KEY =
    "cursor";

// Do not intentionally fill the whole filesystem.
// Actual usable space depends on the board's flash partition layout.
const size_t FLASH_HEADROOM_BYTES =
    128UL * 1024UL;

// Write multiple samples at once rather than doing a flash operation
// for every 100 Hz sensor sample.
const int FLASH_WRITE_BATCH_SIZE =
    32;

SemaphoreHandle_t flashMutex =
    nullptr;

Preferences bufferPreferences;

bool persistentStorageReady =
    false;

size_t persistentReadOffset =
    0;

size_t persistentMaxBytes =
    0;

volatile unsigned long persistentPendingSamples =
    0;

volatile unsigned long persistentWrites =
    0;

volatile unsigned long persistentWriteFailures =
    0;

volatile unsigned long persistentRecoveredAtBoot =
    0;



// ============================================================
// PRE-EVENT RING BUFFER
// ============================================================

CloudSample preEventBuffer[
    PRE_EVENT_SAMPLE_COUNT
];

int preEventWriteIndex = 0;
int preEventCount = 0;

unsigned long forceHighResolutionUntilMs = 0;


// ============================================================
// IMU VARIABLES
// ============================================================

float accX = 0.0f;
float accY = 0.0f;
float accZ = 0.0f;

float gyrX = 0.0f;
float gyrY = 0.0f;
float gyrZ = 0.0f;

unsigned long sampleCounter = 0;

unsigned long lastSampleMs = 0;
unsigned long lastBatteryMs = 0;
unsigned long lastDisplayMs = 0;

volatile uint16_t chargeLevel = 0;


// ============================================================
// GRAVITY / ORIENTATION ESTIMATE
// ============================================================

bool gravityInitialised = false;

float gravityX = 0.0f;
float gravityY = 0.0f;
float gravityZ = 1.0f;

float referenceGravityX = 0.0f;
float referenceGravityY = 0.0f;
float referenceGravityZ = 1.0f;

float postGravityX = 0.0f;
float postGravityY = 0.0f;
float postGravityZ = 1.0f;


// ============================================================
// EDGE DETECTOR STATE
// ============================================================

enum DetectorState {
    MONITORING,
    FREE_FALL_ACTIVE,
    WAITING_FOR_IMPACT,
    ANALYSING_POST_IMPACT
};

DetectorState detectorState =
    MONITORING;

unsigned long stateStartMs = 0;
unsigned long stationaryStartMs = 0;
unsigned long nearMissStartMs = 0;

bool postImpactCameFromFreeFall = false;

float peakAccelerationG = 0.0f;
float peakRotationDps = 0.0f;

unsigned long lastNearMissAlertMs = 0;
unsigned long lastCriticalAlertMs = 0;


// ============================================================
// LOCAL STATUS
// ============================================================

char localStatusText[40] =
    "EDGE MONITORING";

uint16_t localStatusColour =
    TFT_GREEN;

unsigned long localStatusUntilMs = 0;


// ============================================================
// BLE
// ============================================================

bool bleConnected = false;

BLEServer *pServer = nullptr;

BLECharacteristic accelCharacteristic(
    ACCEL_UUID,
    BLECharacteristic::PROPERTY_NOTIFY
);

BLECharacteristic gyroCharacteristic(
    GYRO_UUID,
    BLECharacteristic::PROPERTY_NOTIFY
);

BLECharacteristic accelGyroCharacteristic(
    ACCEL_GYRO_UUID,
    BLECharacteristic::PROPERTY_NOTIFY
);

BLECharacteristic safetyAlertCharacteristic(
    SAFETY_ALERT_UUID,
    BLECharacteristic::PROPERTY_NOTIFY
    |
    BLECharacteristic::PROPERTY_READ
);


class MyServerCallbacks
    : public BLEServerCallbacks {

    void onConnect(
        BLEServer *server
    ) override {

        bleConnected = true;

        Serial.println(
            "BLE client connected"
        );
    }


    void onDisconnect(
        BLEServer *server
    ) override {

        bleConnected = false;

        Serial.println(
            "BLE client disconnected"
        );

        server->startAdvertising();
    }
};


// ============================================================
// HELPERS
// ============================================================

float vectorMagnitude(
    float x,
    float y,
    float z
) {

    return sqrtf(
        x * x
        +
        y * y
        +
        z * z
    );
}


void setLocalStatus(
    const char *text,
    uint16_t colour,
    unsigned long holdMs
) {

    snprintf(
        localStatusText,
        sizeof(
            localStatusText
        ),
        "%s",
        text
    );

    localStatusColour =
        colour;

    localStatusUntilMs =
        millis()
        +
        holdMs;
}


void restoreMonitoringStatusIfNeeded(
    unsigned long now
) {

    if (
        localStatusUntilMs != 0
        &&
        (long)(
            now
            -
            localStatusUntilMs
        )
        >= 0
    ) {

        snprintf(
            localStatusText,
            sizeof(
                localStatusText
            ),
            "%s",
            "EDGE MONITORING"
        );

        localStatusColour =
            TFT_GREEN;

        localStatusUntilMs = 0;
    }
}


// ============================================================
// CLOUD QUEUE HELPERS
// ============================================================

CloudSample makeCurrentSample() {

    CloudSample sample;

    sample.t =
        millis()
        /
        1000.0f;

    sample.ax = accX;
    sample.ay = accY;
    sample.az = accZ;

    sample.gx = gyrX;
    sample.gy = gyrY;
    sample.gz = gyrZ;

    return sample;
}


void saveToPreEventBuffer(
    const CloudSample &sample
) {

    preEventBuffer[
        preEventWriteIndex
    ] = sample;

    preEventWriteIndex =
        (
            preEventWriteIndex
            +
            1
        )
        %
        PRE_EVENT_SAMPLE_COUNT;

    if (
        preEventCount
        <
        PRE_EVENT_SAMPLE_COUNT
    ) {

        preEventCount++;
    }
}


bool enqueueCloudSample(
    const CloudSample &sample,
    bool important
) {

    if (
        cloudQueue
            ==
            nullptr
    ) {

        return false;
    }


    if (
        xQueueSend(
            cloudQueue,
            &sample,
            0
        )
        ==
        pdTRUE
    ) {

        return true;
    }


    if (important) {
        droppedImportantSamples++;
    } else {
        droppedNormalSamples++;
    }

    return false;
}


void flushPreEventBufferToCloudQueue() {

    if (
        preEventCount
            <=
            0
    ) {

        return;
    }


    int oldestIndex =
        (
            preEventWriteIndex
            -
            preEventCount
            +
            PRE_EVENT_SAMPLE_COUNT
        )
        %
        PRE_EVENT_SAMPLE_COUNT;


    Serial.print(
        "EDGE: queueing "
    );

    Serial.print(
        preEventCount
    );

    Serial.println(
        " pre-event samples"
    );


    for (
        int i = 0;
        i < preEventCount;
        i++
    ) {

        int index =
            (
                oldestIndex
                +
                i
            )
            %
            PRE_EVENT_SAMPLE_COUNT;


        enqueueCloudSample(
            preEventBuffer[index],
            true
        );
    }
}


// ============================================================
// PERSISTENT FLASH BUFFER HELPERS
// ============================================================

bool lockFlash(
    TickType_t timeoutTicks =
        pdMS_TO_TICKS(
            2000
        )
) {

    if (
        flashMutex
            ==
            nullptr
    ) {

        return false;
    }


    return xSemaphoreTake(
        flashMutex,
        timeoutTicks
    )
    ==
    pdTRUE;
}


void unlockFlash() {

    if (
        flashMutex
            !=
            nullptr
    ) {

        xSemaphoreGive(
            flashMutex
        );
    }
}


size_t getOfflineFileSizeLocked() {

    if (
        !LittleFS.exists(
            OFFLINE_BUFFER_FILE
        )
    ) {

        return 0;
    }


    File file =
        LittleFS.open(
            OFFLINE_BUFFER_FILE,
            FILE_READ
        );


    if (!file) {
        return 0;
    }


    size_t size =
        file.size();

    file.close();

    return size;
}


void savePersistentCursorLocked() {

    bufferPreferences.putULong(
        NVS_CURSOR_KEY,
        (uint32_t)
        persistentReadOffset
    );
}


void resetPersistentBufferLocked() {

    if (
        LittleFS.exists(
            OFFLINE_BUFFER_FILE
        )
    ) {

        LittleFS.remove(
            OFFLINE_BUFFER_FILE
        );
    }


    persistentReadOffset =
        0;

    persistentPendingSamples =
        0;

    savePersistentCursorLocked();
}


bool setupPersistentStorage() {

    flashMutex =
        xSemaphoreCreateMutex();


    if (
        flashMutex
            ==
            nullptr
    ) {

        Serial.println(
            "FLASH: could not create mutex"
        );

        return false;
    }


    // First try to mount without formatting so existing buffered
    // safety data is preserved.
    if (
        !LittleFS.begin(
            false
        )
    ) {

        Serial.println(
            "FLASH: LittleFS mount failed"
        );

        Serial.println(
            "FLASH: attempting first-time format"
        );


        // This is intended for first-time setup when no valid
        // filesystem exists. If formatting also fails, RAM-only
        // buffering remains available.
        if (
            !LittleFS.begin(
                true
            )
        ) {

            Serial.println(
                "FLASH: persistent buffer unavailable"
            );

            return false;
        }
    }


    if (
        !bufferPreferences.begin(
            NVS_NAMESPACE,
            false
        )
    ) {

        Serial.println(
            "FLASH: NVS Preferences unavailable"
        );

        LittleFS.end();

        return false;
    }


    size_t totalBytes =
        LittleFS.totalBytes();


    if (
        totalBytes
            >
            FLASH_HEADROOM_BYTES
    ) {

        persistentMaxBytes =
            totalBytes
            -
            FLASH_HEADROOM_BYTES;
    }

    else {

        persistentMaxBytes =
            totalBytes;
    }


    persistentReadOffset =
        bufferPreferences.getULong(
            NVS_CURSOR_KEY,
            0
        );


    if (!lockFlash()) {

        Serial.println(
            "FLASH: mutex failed during startup"
        );

        return false;
    }


    size_t fileSize =
        getOfflineFileSizeLocked();


    // Recover safely from an invalid cursor.
    if (
        persistentReadOffset
            >
            fileSize
        ||
        (
            persistentReadOffset
            %
            sizeof(
                CloudSample
            )
        )
            !=
            0
    ) {

        Serial.println(
            "FLASH: invalid cursor reset to 0"
        );

        persistentReadOffset =
            0;

        savePersistentCursorLocked();
    }


    size_t pendingBytes =
        fileSize
        -
        persistentReadOffset;


    persistentPendingSamples =
        pendingBytes
        /
        sizeof(
            CloudSample
        );


    persistentRecoveredAtBoot =
        persistentPendingSamples;


    // If an old file is already fully consumed, clean it up.
    if (
        fileSize
            >
            0
        &&
        persistentPendingSamples
            ==
            0
    ) {

        resetPersistentBufferLocked();
    }


    unlockFlash();


    Serial.print(
        "FLASH: LittleFS total bytes = "
    );

    Serial.println(
        (unsigned long)
        totalBytes
    );


    Serial.print(
        "FLASH: LittleFS used bytes = "
    );

    Serial.println(
        (unsigned long)
        LittleFS.usedBytes()
    );


    Serial.print(
        "FLASH: recovered pending samples = "
    );

    Serial.println(
        persistentRecoveredAtBoot
    );


    return true;
}


bool appendSamplesToPersistentFlash(
    CloudSample *samples,
    int count
) {

    if (
        !persistentStorageReady
        ||
        count
            <=
            0
    ) {

        return false;
    }


    if (!lockFlash()) {

        persistentWriteFailures++;

        return false;
    }


    size_t currentSize =
        getOfflineFileSizeLocked();


    size_t bytesToWrite =
        (
            size_t
        )
        count
        *
        sizeof(
            CloudSample
        );


    if (
        currentSize
        +
        bytesToWrite
        >
        persistentMaxBytes
    ) {

        Serial.println(
            "FLASH: offline buffer full"
        );

        persistentWriteFailures++;

        unlockFlash();

        return false;
    }


    File file =
        LittleFS.open(
            OFFLINE_BUFFER_FILE,
            FILE_APPEND
        );


    if (!file) {

        Serial.println(
            "FLASH: could not open offline buffer"
        );

        persistentWriteFailures++;

        unlockFlash();

        return false;
    }


    size_t written =
        file.write(
            (
                const uint8_t *
            )
            samples,
            bytesToWrite
        );


    file.flush();
    file.close();


    bool success =
        written
        ==
        bytesToWrite;


    if (success) {

        persistentPendingSamples +=
            count;

        persistentWrites++;
    }

    else {

        Serial.println(
            "FLASH: incomplete offline write"
        );

        persistentWriteFailures++;
    }


    unlockFlash();

    return success;
}


int readPersistentBatch(
    CloudSample *destination,
    int maxCount
) {

    if (
        !persistentStorageReady
        ||
        maxCount
            <=
            0
        ||
        persistentPendingSamples
            ==
            0
    ) {

        return 0;
    }


    if (!lockFlash()) {
        return 0;
    }


    File file =
        LittleFS.open(
            OFFLINE_BUFFER_FILE,
            FILE_READ
        );


    if (!file) {

        unlockFlash();

        return 0;
    }


    size_t fileSize =
        file.size();


    if (
        persistentReadOffset
            >=
            fileSize
    ) {

        file.close();

        resetPersistentBufferLocked();

        unlockFlash();

        return 0;
    }


    if (
        !file.seek(
            persistentReadOffset,
            SeekSet
        )
    ) {

        file.close();

        unlockFlash();

        return 0;
    }


    size_t remainingBytes =
        fileSize
        -
        persistentReadOffset;


    int availableRecords =
        remainingBytes
        /
        sizeof(
            CloudSample
        );


    int count =
        min(
            maxCount,
            availableRecords
        );


    size_t requestedBytes =
        (
            size_t
        )
        count
        *
        sizeof(
            CloudSample
        );


    size_t bytesRead =
        file.read(
            (
                uint8_t *
            )
            destination,
            requestedBytes
        );


    file.close();

    unlockFlash();


    return bytesRead
        /
        sizeof(
            CloudSample
        );
}


void confirmPersistentBatchUploaded(
    int uploadedCount
) {

    if (
        !persistentStorageReady
        ||
        uploadedCount
            <=
            0
    ) {

        return;
    }


    if (!lockFlash()) {
        return;
    }


    persistentReadOffset +=
        (
            size_t
        )
        uploadedCount
        *
        sizeof(
            CloudSample
        );


    if (
        persistentPendingSamples
            >=
            (
                unsigned long
            )
            uploadedCount
    ) {

        persistentPendingSamples -=
            uploadedCount;
    }

    else {

        persistentPendingSamples =
            0;
    }


    savePersistentCursorLocked();


    size_t fileSize =
        getOfflineFileSizeLocked();


    if (
        persistentPendingSamples
            ==
            0
        ||
        persistentReadOffset
            >=
            fileSize
    ) {

        Serial.println(
            "FLASH: offline backlog fully synced"
        );

        resetPersistentBufferLocked();
    }


    unlockFlash();
}


void spillRamQueueToPersistentFlash() {

    if (
        cloudQueue
            ==
            nullptr
    ) {

        return;
    }


    // If the filesystem cannot be mounted, leave samples in RAM.
    if (!persistentStorageReady) {
        return;
    }


    CloudSample flashBatch[
        FLASH_WRITE_BATCH_SIZE
    ];

    int count =
        0;


    CloudSample incoming;


    while (
        count
            <
            FLASH_WRITE_BATCH_SIZE
        &&
        xQueueReceive(
            cloudQueue,
            &incoming,
            0
        )
        ==
        pdTRUE
    ) {

        flashBatch[
            count
        ] =
            incoming;

        count++;
    }


    if (
        count
            ==
            0
    ) {

        return;
    }


    if (
        appendSamplesToPersistentFlash(
            flashBatch,
            count
        )
    ) {

        Serial.print(
            "FLASH: saved "
        );

        Serial.print(
            count
        );

        Serial.print(
            " samples | pending "
        );

        Serial.println(
            persistentPendingSamples
        );

        return;
    }


    // Flash write failed. Put samples back into RAM in reverse
    // so their original order is retained as much as possible.
    for (
        int i =
            count
            -
            1;
        i >= 0;
        i--
    ) {

        if (
            xQueueSendToFront(
                cloudQueue,
                &flashBatch[i],
                0
            )
            !=
            pdTRUE
        ) {

            droppedImportantSamples++;
        }
    }
}


// ============================================================
// BLE SETUP
// ============================================================

void setupBLE() {

    BLEDevice::init(
        BLE_SERVER_NAME
    );

    BLEDevice::setMTU(
        128
    );


    pServer =
        BLEDevice::createServer();

    pServer->setCallbacks(
        new MyServerCallbacks()
    );


    BLEService *service =
        pServer->createService(
            SERVICE_UUID
        );


    BLEDescriptor *accelDescriptor =
        new BLEDescriptor(
            BLEUUID(
                (uint16_t)0x2901
            )
        );

    accelDescriptor->setValue(
        "Accelerometer"
    );

    accelCharacteristic.addDescriptor(
        accelDescriptor
    );

    accelCharacteristic.addDescriptor(
        new BLE2902()
    );

    service->addCharacteristic(
        &accelCharacteristic
    );


    BLEDescriptor *gyroDescriptor =
        new BLEDescriptor(
            BLEUUID(
                (uint16_t)0x2901
            )
        );

    gyroDescriptor->setValue(
        "Gyroscope"
    );

    gyroCharacteristic.addDescriptor(
        gyroDescriptor
    );

    gyroCharacteristic.addDescriptor(
        new BLE2902()
    );

    service->addCharacteristic(
        &gyroCharacteristic
    );


    BLEDescriptor *combinedDescriptor =
        new BLEDescriptor(
            BLEUUID(
                (uint16_t)0x2901
            )
        );

    combinedDescriptor->setValue(
        "AccelerometerAndGyroscope"
    );

    accelGyroCharacteristic.addDescriptor(
        combinedDescriptor
    );

    accelGyroCharacteristic.addDescriptor(
        new BLE2902()
    );

    service->addCharacteristic(
        &accelGyroCharacteristic
    );


    BLEDescriptor *alertDescriptor =
        new BLEDescriptor(
            BLEUUID(
                (uint16_t)0x2901
            )
        );

    alertDescriptor->setValue(
        "EdgeSafetyAlert"
    );

    safetyAlertCharacteristic.addDescriptor(
        alertDescriptor
    );

    safetyAlertCharacteristic.addDescriptor(
        new BLE2902()
    );

    safetyAlertCharacteristic.setValue(
        "READY,100,0"
    );

    service->addCharacteristic(
        &safetyAlertCharacteristic
    );


    service->start();


    BLEAdvertising *advertising =
        BLEDevice::getAdvertising();

    advertising->addServiceUUID(
        SERVICE_UUID
    );

    advertising->setScanResponse(
        true
    );

    BLEDevice::startAdvertising();


    Serial.println(
        "BLE advertising started"
    );
}


// ============================================================
// BLE RAW SENSOR SEND
// ============================================================

void sendBLE() {

    if (!bleConnected) {
        return;
    }


    char accelBuffer[
        60
    ];

    snprintf(
        accelBuffer,
        sizeof(
            accelBuffer
        ),
        "%.3f,%.3f,%.3f",
        accX,
        accY,
        accZ
    );

    accelCharacteristic.setValue(
        accelBuffer
    );

    accelCharacteristic.notify();


    char gyroBuffer[
        60
    ];

    snprintf(
        gyroBuffer,
        sizeof(
            gyroBuffer
        ),
        "%.2f,%.2f,%.2f",
        gyrX,
        gyrY,
        gyrZ
    );

    gyroCharacteristic.setValue(
        gyroBuffer
    );

    gyroCharacteristic.notify();


    char combinedBuffer[
        100
    ];

    snprintf(
        combinedBuffer,
        sizeof(
            combinedBuffer
        ),
        "%.3f,%.3f,%.3f,"
        "%.2f,%.2f,%.2f",
        accX,
        accY,
        accZ,
        gyrX,
        gyrY,
        gyrZ
    );

    accelGyroCharacteristic.setValue(
        combinedBuffer
    );

    accelGyroCharacteristic.notify();
}


// ============================================================
// ORIENTATION
// ============================================================

void captureReferenceOrientation() {

    referenceGravityX =
        gravityX;

    referenceGravityY =
        gravityY;

    referenceGravityZ =
        gravityZ;
}


float calculateOrientationChangeDegrees() {

    float referenceMagnitude =
        vectorMagnitude(
            referenceGravityX,
            referenceGravityY,
            referenceGravityZ
        );


    float postMagnitude =
        vectorMagnitude(
            postGravityX,
            postGravityY,
            postGravityZ
        );


    if (
        referenceMagnitude
            <
            0.01f
        ||
        postMagnitude
            <
            0.01f
    ) {

        return 0.0f;
    }


    float dotProduct =
        (
            referenceGravityX
            *
            postGravityX
        )
        +
        (
            referenceGravityY
            *
            postGravityY
        )
        +
        (
            referenceGravityZ
            *
            postGravityZ
        );


    float cosineAngle =
        dotProduct
        /
        (
            referenceMagnitude
            *
            postMagnitude
        );


    cosineAngle =
        constrain(
            cosineAngle,
            -1.0f,
            1.0f
        );


    return acosf(
        cosineAngle
    )
    *
    57.2957795f;
}


// ============================================================
// LOCAL ALERT
// ============================================================

void raiseLocalAlert(
    const char *eventType,
    int confidence,
    float accelerationMagnitude,
    float rotationMagnitude,
    bool critical
) {

    unsigned long now =
        millis();


    if (critical) {

        if (
            lastCriticalAlertMs != 0
            &&
            now
            -
            lastCriticalAlertMs
            <
            CRITICAL_ALERT_COOLDOWN_MS
        ) {

            return;
        }

        lastCriticalAlertMs =
            now;
    }

    else {

        if (
            lastNearMissAlertMs != 0
            &&
            now
            -
            lastNearMissAlertMs
            <
            NEAR_MISS_COOLDOWN_MS
        ) {

            return;
        }

        lastNearMissAlertMs =
            now;
    }


    // Keep high-resolution post-event samples.
    forceHighResolutionUntilMs =
        now
        +
        POST_EVENT_HIGH_RES_MS;


    // If the event happened while disconnected, push the local
    // 3-second pre-event ring buffer into the RAM upload queue.
    if (
        WiFi.status()
        != WL_CONNECTED
    ) {

        flushPreEventBufferToCloudQueue();
    }


    // BLE alert packet:
    // EVENT,confidence,time_ms
    char alertBuffer[
        64
    ];

    snprintf(
        alertBuffer,
        sizeof(
            alertBuffer
        ),
        "%s,%d,%lu",
        eventType,
        confidence,
        now
    );


    safetyAlertCharacteristic.setValue(
        alertBuffer
    );


    if (bleConnected) {

        safetyAlertCharacteristic.notify();
    }


    Serial.println();
    Serial.println(
        "========================================"
    );

    Serial.println(
        "EDGE SAFETY ALERT"
    );

    Serial.print(
        "Event: "
    );

    Serial.println(
        eventType
    );

    Serial.print(
        "Confidence: "
    );

    Serial.print(
        confidence
    );

    Serial.println(
        "%"
    );

    Serial.print(
        "Peak acceleration: "
    );

    Serial.print(
        accelerationMagnitude,
        2
    );

    Serial.println(
        " g"
    );

    Serial.print(
        "Peak rotation: "
    );

    Serial.print(
        rotationMagnitude,
        1
    );

    Serial.println(
        " deg/s"
    );

    Serial.print(
        "WiFi at event: "
    );

    Serial.println(
        WiFi.status()
            ==
            WL_CONNECTED
        ?
        "ONLINE"
        :
        "OFFLINE - EVENT BUFFERED"
    );

    Serial.println(
        "========================================"
    );

    Serial.println();


    if (
        strcmp(
            eventType,
            "FFH"
        )
        ==
        0
    ) {

        setLocalStatus(
            "FFH DETECTED - CHECK WORKER",
            TFT_RED,
            12000
        );
    }

    else if (
        strcmp(
            eventType,
            "STF"
        )
        ==
        0
    ) {

        setLocalStatus(
            "STF DETECTED - CHECK WORKER",
            TFT_RED,
            12000
        );
    }

    else {

        setLocalStatus(
            "NEAR MISS DETECTED",
            TFT_YELLOW,
            8000
        );
    }
}


// ============================================================
// DETECTOR HELPERS
// ============================================================

void resetDetector() {

    detectorState =
        MONITORING;

    stateStartMs = 0;
    stationaryStartMs = 0;
    nearMissStartMs = 0;

    postImpactCameFromFreeFall =
        false;

    peakAccelerationG = 0.0f;
    peakRotationDps = 0.0f;
}


void beginPostImpactAnalysis(
    bool cameFromFreeFall,
    unsigned long now,
    float accelerationMagnitude,
    float rotationMagnitude
) {

    detectorState =
        ANALYSING_POST_IMPACT;

    stateStartMs =
        now;

    stationaryStartMs =
        0;

    postImpactCameFromFreeFall =
        cameFromFreeFall;

    peakAccelerationG =
        accelerationMagnitude;

    peakRotationDps =
        rotationMagnitude;

    postGravityX = accX;
    postGravityY = accY;
    postGravityZ = accZ;


    Serial.println(
        "EDGE: impact detected, analysing..."
    );

    setLocalStatus(
        "ANALYSING POSSIBLE FALL",
        TFT_YELLOW,
        POST_IMPACT_MAX_MS
        +
        1000
    );
}


// ============================================================
// EDGE SAFETY DETECTOR
// ============================================================

void updateSafetyDetector(
    unsigned long now,
    float accelerationMagnitude,
    float rotationMagnitude
) {

    // Slow gravity estimate.
    if (!gravityInitialised) {

        gravityX = accX;
        gravityY = accY;
        gravityZ = accZ;

        gravityInitialised =
            true;
    }

    else {

        float alpha =
            0.94f;


        if (
            accelerationMagnitude
                <
                0.70f
            ||
            accelerationMagnitude
                >
                1.30f
            ||
            rotationMagnitude
                >
                80.0f
        ) {

            alpha =
                0.995f;
        }


        gravityX =
            (
                alpha
                *
                gravityX
            )
            +
            (
                (
                    1.0f
                    -
                    alpha
                )
                *
                accX
            );


        gravityY =
            (
                alpha
                *
                gravityY
            )
            +
            (
                (
                    1.0f
                    -
                    alpha
                )
                *
                accY
            );


        gravityZ =
            (
                alpha
                *
                gravityZ
            )
            +
            (
                (
                    1.0f
                    -
                    alpha
                )
                *
                accZ
            );
    }


    switch (
        detectorState
    ) {

        case MONITORING: {

            // 1. Possible free fall.
            if (
                accelerationMagnitude
                    <=
                    FREE_FALL_THRESHOLD_G
            ) {

                captureReferenceOrientation();

                detectorState =
                    FREE_FALL_ACTIVE;

                stateStartMs =
                    now;

                nearMissStartMs =
                    0;

                Serial.println(
                    "EDGE: possible free fall started"
                );

                setLocalStatus(
                    "POSSIBLE FREE FALL",
                    TFT_YELLOW,
                    2500
                );

                return;
            }


            // 2. Sudden impact without a confirmed free-fall phase.
            // This may become STF after post-impact analysis.
            if (
                accelerationMagnitude
                    >=
                    HARD_IMPACT_THRESHOLD_G
                &&
                rotationMagnitude
                    >=
                    STF_ROTATION_DPS
            ) {

                captureReferenceOrientation();

                beginPostImpactAnalysis(
                    false,
                    now,
                    accelerationMagnitude,
                    rotationMagnitude
                );

                return;
            }


            // 3. Strong loss-of-balance movement.
            bool riskyMovement =
                rotationMagnitude
                    >=
                    NEAR_MISS_ROTATION_DPS
                &&
                (
                    accelerationMagnitude
                        >=
                        NEAR_MISS_HIGH_ACCEL_G
                    ||
                    accelerationMagnitude
                        <=
                        NEAR_MISS_LOW_ACCEL_G
                );


            if (riskyMovement) {

                if (
                    nearMissStartMs
                        ==
                        0
                ) {

                    nearMissStartMs =
                        now;

                    peakAccelerationG =
                        accelerationMagnitude;

                    peakRotationDps =
                        rotationMagnitude;
                }

                else {

                    if (
                        accelerationMagnitude
                            >
                            peakAccelerationG
                    ) {

                        peakAccelerationG =
                            accelerationMagnitude;
                    }


                    if (
                        rotationMagnitude
                            >
                            peakRotationDps
                    ) {

                        peakRotationDps =
                            rotationMagnitude;
                    }


                    if (
                        now
                        -
                        nearMissStartMs
                        >=
                        NEAR_MISS_MIN_MS
                    ) {

                        int confidence =
                            60;


                        if (
                            peakRotationDps
                                >=
                                300.0f
                        ) {

                            confidence +=
                                8;
                        }


                        if (
                            peakAccelerationG
                                >=
                                2.0f
                        ) {

                            confidence +=
                                7;
                        }


                        if (
                            confidence
                                >
                                75
                        ) {

                            confidence =
                                75;
                        }


                        raiseLocalAlert(
                            "NEAR_MISS",
                            confidence,
                            peakAccelerationG,
                            peakRotationDps,
                            false
                        );


                        nearMissStartMs =
                            0;

                        peakAccelerationG =
                            0.0f;

                        peakRotationDps =
                            0.0f;
                    }
                }
            }

            else if (
                nearMissStartMs
                    !=
                    0
                &&
                now
                -
                nearMissStartMs
                >
                300
            ) {

                nearMissStartMs =
                    0;

                peakAccelerationG =
                    0.0f;

                peakRotationDps =
                    0.0f;
            }


            break;
        }


        case FREE_FALL_ACTIVE: {

            if (
                accelerationMagnitude
                    <=
                    FREE_FALL_THRESHOLD_G
            ) {

                return;
            }


            unsigned long freeFallDuration =
                now
                -
                stateStartMs;


            if (
                freeFallDuration
                    >=
                    FREE_FALL_MIN_MS
            ) {

                detectorState =
                    WAITING_FOR_IMPACT;

                stateStartMs =
                    now;


                Serial.print(
                    "EDGE: free fall confirmed for "
                );

                Serial.print(
                    freeFallDuration
                );

                Serial.println(
                    " ms; waiting for impact"
                );


                setLocalStatus(
                    "FREE FALL - WAITING IMPACT",
                    TFT_YELLOW,
                    IMPACT_WAIT_MS
                    +
                    1000
                );
            }

            else {

                resetDetector();
            }


            break;
        }


        case WAITING_FOR_IMPACT: {

            if (
                accelerationMagnitude
                    >
                    peakAccelerationG
            ) {

                peakAccelerationG =
                    accelerationMagnitude;
            }


            if (
                rotationMagnitude
                    >
                    peakRotationDps
            ) {

                peakRotationDps =
                    rotationMagnitude;
            }


            if (
                accelerationMagnitude
                    >=
                    HARD_IMPACT_THRESHOLD_G
                &&
                rotationMagnitude
                    >=
                    FFH_ROTATION_DPS
            ) {

                beginPostImpactAnalysis(
                    true,
                    now,
                    accelerationMagnitude,
                    rotationMagnitude
                );

                return;
            }


            if (
                now
                -
                stateStartMs
                >=
                IMPACT_WAIT_MS
            ) {

                // Free-fall-like motion without the full impact sequence.
                raiseLocalAlert(
                    "NEAR_MISS",
                    65,
                    peakAccelerationG,
                    peakRotationDps,
                    false
                );

                resetDetector();
            }


            break;
        }


        case ANALYSING_POST_IMPACT: {

            if (
                accelerationMagnitude
                    >
                    peakAccelerationG
            ) {

                peakAccelerationG =
                    accelerationMagnitude;
            }


            if (
                rotationMagnitude
                    >
                    peakRotationDps
            ) {

                peakRotationDps =
                    rotationMagnitude;
            }


            if (
                accelerationMagnitude
                    >=
                    0.70f
                &&
                accelerationMagnitude
                    <=
                    1.30f
            ) {

                const float postAlpha =
                    0.80f;


                postGravityX =
                    (
                        postAlpha
                        *
                        postGravityX
                    )
                    +
                    (
                        (
                            1.0f
                            -
                            postAlpha
                        )
                        *
                        accX
                    );


                postGravityY =
                    (
                        postAlpha
                        *
                        postGravityY
                    )
                    +
                    (
                        (
                            1.0f
                            -
                            postAlpha
                        )
                        *
                        accY
                    );


                postGravityZ =
                    (
                        postAlpha
                        *
                        postGravityZ
                    )
                    +
                    (
                        (
                            1.0f
                            -
                            postAlpha
                        )
                        *
                        accZ
                    );
            }


            bool currentlyStationary =
                fabsf(
                    accelerationMagnitude
                    -
                    1.0f
                )
                <=
                STATIONARY_ACCEL_TOLERANCE_G
                &&
                rotationMagnitude
                    <=
                    STATIONARY_GYRO_DPS;


            if (currentlyStationary) {

                if (
                    stationaryStartMs
                        ==
                        0
                ) {

                    stationaryStartMs =
                        now;
                }
            }

            else {

                stationaryStartMs =
                    0;
            }


            unsigned long analysisDuration =
                now
                -
                stateStartMs;


            unsigned long stationaryDuration =
                stationaryStartMs
                    ==
                    0
                ?
                0
                :
                now
                -
                stationaryStartMs;


            if (
                analysisDuration
                    >=
                    POST_IMPACT_ANALYSIS_MS
                ||
                analysisDuration
                    >=
                    POST_IMPACT_MAX_MS
            ) {

                float orientationChange =
                    calculateOrientationChangeDegrees();


                bool orientationChanged =
                    orientationChange
                        >=
                        FALL_ORIENTATION_CHANGE_DEG;


                bool movementStopped =
                    stationaryDuration
                        >=
                        REQUIRED_STATIONARY_MS;


                if (
                    postImpactCameFromFreeFall
                ) {

                    int confidence =
                        80;


                    if (movementStopped) {
                        confidence += 8;
                    }


                    if (orientationChanged) {
                        confidence += 7;
                    }


                    if (
                        peakAccelerationG
                            >=
                            3.0f
                    ) {

                        confidence += 5;
                    }


                    if (
                        confidence
                            >
                            98
                    ) {

                        confidence =
                            98;
                    }


                    raiseLocalAlert(
                        "FFH",
                        confidence,
                        peakAccelerationG,
                        peakRotationDps,
                        true
                    );
                }

                else if (
                    peakAccelerationG
                        >=
                        HARD_IMPACT_THRESHOLD_G
                    &&
                    peakRotationDps
                        >=
                        STF_ROTATION_DPS
                    &&
                    (
                        movementStopped
                        ||
                        orientationChanged
                    )
                ) {

                    int confidence =
                        72;


                    if (movementStopped) {
                        confidence += 10;
                    }


                    if (orientationChanged) {
                        confidence += 8;
                    }


                    if (
                        peakAccelerationG
                            >=
                            3.0f
                    ) {

                        confidence += 5;
                    }


                    if (
                        confidence
                            >
                            95
                    ) {

                        confidence =
                            95;
                    }


                    raiseLocalAlert(
                        "STF",
                        confidence,
                        peakAccelerationG,
                        peakRotationDps,
                        true
                    );
                }

                else {

                    int confidence =
                        55;


                    if (
                        peakAccelerationG
                            >=
                            HARD_IMPACT_THRESHOLD_G
                    ) {

                        confidence += 8;
                    }


                    if (
                        peakRotationDps
                            >=
                            300.0f
                    ) {

                        confidence += 7;
                    }


                    if (
                        confidence
                            >
                            75
                    ) {

                        confidence =
                            75;
                    }


                    raiseLocalAlert(
                        "NEAR_MISS",
                        confidence,
                        peakAccelerationG,
                        peakRotationDps,
                        false
                    );
                }


                Serial.print(
                    "EDGE orientation change: "
                );

                Serial.print(
                    orientationChange,
                    1
                );

                Serial.println(
                    " degrees"
                );


                Serial.print(
                    "EDGE stationary duration: "
                );

                Serial.print(
                    stationaryDuration
                );

                Serial.println(
                    " ms"
                );


                resetDetector();
            }


            break;
        }
    }
}


// ============================================================
// CLOUD-SAMPLE SELECTION
// ============================================================

bool shouldQueueCurrentSample(
    float accelerationMagnitude,
    float rotationMagnitude,
    unsigned long now,
    bool &important
) {

    bool thresholdImportant =
        accelerationMagnitude
            >=
            1.80f
        ||
        accelerationMagnitude
            <=
            0.60f
        ||
        rotationMagnitude
            >=
            140.0f;


    bool eventHighResolution =
        forceHighResolutionUntilMs
            !=
            0
        &&
        (long)(
            forceHighResolutionUntilMs
            -
            now
        )
        >
        0;


    important =
        thresholdImportant
        ||
        eventHighResolution;


    if (important) {
        return true;
    }


    if (
        WiFi.status()
        ==
        WL_CONNECTED
    ) {

        return (
            sampleCounter
            %
            ONLINE_NORMAL_DIVIDER
        )
        ==
        0;
    }


    return (
        sampleCounter
        %
        OFFLINE_NORMAL_DIVIDER
    )
    ==
    0;
}


// ============================================================
// HTTPS UPLOAD
// Runs only from the network task.
// ============================================================

bool uploadBatchToRender(
    CloudSample *batch,
    int batchCount
) {

    if (
        batchCount
            <=
            0
    ) {

        return true;
    }


    if (
        WiFi.status()
            !=
            WL_CONNECTED
    ) {

        return false;
    }


    WiFiClientSecure client;

    // Classroom prototype:
    // connection is encrypted but server certificate validation
    // is disabled. For production, replace with a CA certificate.
    client.setInsecure();


    HTTPClient http;

    // Keep the network task responsive.
    http.setTimeout(
        5000
    );


    if (
        !http.begin(
            client,
            API_URL
        )
    ) {

        return false;
    }


    http.addHeader(
        "Content-Type",
        "application/json"
    );

    http.addHeader(
        "X-API-Key",
        API_KEY
    );


    String payload;

    payload.reserve(
        7000
    );


    payload +=
        "{\"device_name\":\"";

    payload +=
        BLE_SERVER_NAME;

    payload +=
        "\",\"worker_id\":\"";

    payload +=
        WORKER_ID;

    payload +=
        "\",\"battery_percent\":";

    payload +=
        String(
            (uint16_t)chargeLevel
        );

    payload +=
        ",\"samples\":[";


    for (
        int i = 0;
        i < batchCount;
        i++
    ) {

        if (i > 0) {
            payload += ",";
        }


        CloudSample &sample =
            batch[i];


        payload +=
            "{\"t\":";

        payload +=
            String(
                sample.t,
                3
            );

        payload +=
            ",\"ax\":";

        payload +=
            String(
                sample.ax,
                4
            );

        payload +=
            ",\"ay\":";

        payload +=
            String(
                sample.ay,
                4
            );

        payload +=
            ",\"az\":";

        payload +=
            String(
                sample.az,
                4
            );

        payload +=
            ",\"gx\":";

        payload +=
            String(
                sample.gx,
                2
            );

        payload +=
            ",\"gy\":";

        payload +=
            String(
                sample.gy,
                2
            );

        payload +=
            ",\"gz\":";

        payload +=
            String(
                sample.gz,
                2
            );

        payload +=
            "}";
    }


    payload +=
        "]}";


    int code =
        http.POST(
            payload
        );


    Serial.print(
        "Render HTTP: "
    );

    Serial.println(
        code
    );


    bool success =
        code
            >=
            200
        &&
        code
            <
            300;


    if (success) {

        Serial.print(
            "Cloud batch uploaded: "
        );

        Serial.print(
            batchCount
        );

        Serial.print(
            " | queued: "
        );

        Serial.println(
            cloudQueue
                ?
                uxQueueMessagesWaiting(
                    cloudQueue
                )
                :
                0
        );
    }

    else if (
        code
            >
            0
    ) {

        Serial.println(
            http.getString()
        );
    }


    http.end();

    return success;
}


// ============================================================
// NETWORK TASK
// Separate task keeps Wi-Fi / HTTPS / flash I/O away from the
// 100 Hz edge detector.
// ============================================================

void networkTask(
    void *parameter
) {

    WiFi.mode(
        WIFI_STA
    );


    unsigned long lastWifiAttemptMs =
        0;

    unsigned long nextHttpAllowedMs =
        0;

    unsigned long firstRamBatchSampleMs =
        0;


    CloudSample ramBatch[
        CLOUD_BATCH_SIZE
    ];

    int ramBatchCount =
        0;


    CloudSample flashUploadBatch[
        CLOUD_BATCH_SIZE
    ];


    while (true) {

        unsigned long now =
            millis();


        // ----------------------------------------------------
        // OFFLINE:
        // 1. Keep trying Wi-Fi.
        // 2. Spill RAM queue to LittleFS so the backlog survives
        //    a reset or temporary power loss.
        // ----------------------------------------------------
        if (
            WiFi.status()
                !=
                WL_CONNECTED
        ) {

            spillRamQueueToPersistentFlash();


            if (
                lastWifiAttemptMs
                    ==
                    0
                ||
                now
                -
                lastWifiAttemptMs
                >=
                WIFI_RETRY_MS
            ) {

                lastWifiAttemptMs =
                    now;


                Serial.print(
                    "WiFi: trying "
                );

                Serial.println(
                    WIFI_SSID
                );


                WiFi.disconnect(
                    false
                );

                WiFi.begin(
                    WIFI_SSID,
                    WIFI_PASSWORD
                );
            }


            vTaskDelay(
                pdMS_TO_TICKS(
                    50
                )
            );

            continue;
        }


        // ----------------------------------------------------
        // ONLINE + FLASH BACKLOG:
        // Old offline records are uploaded BEFORE new RAM data.
        // This prevents historical safety data from being stuck
        // behind the live stream.
        // ----------------------------------------------------
        if (
            persistentStorageReady
            &&
            persistentPendingSamples
                >
                0
        ) {

            if (
                (long)(
                    now
                    -
                    nextHttpAllowedMs
                )
                >=
                0
            ) {

                int flashCount =
                    readPersistentBatch(
                        flashUploadBatch,
                        CLOUD_BATCH_SIZE
                    );


                if (
                    flashCount
                        >
                        0
                ) {

                    if (
                        uploadBatchToRender(
                            flashUploadBatch,
                            flashCount
                        )
                    ) {

                        successfulCloudBatches++;

                        confirmPersistentBatchUploaded(
                            flashCount
                        );

                        // Send backlog relatively quickly while still
                        // avoiding a tight HTTP loop.
                        nextHttpAllowedMs =
                            millis()
                            +
                            250;
                    }

                    else {

                        failedCloudBatches++;

                        nextHttpAllowedMs =
                            millis()
                            +
                            HTTP_RETRY_MS;
                    }
                }
            }


            vTaskDelay(
                pdMS_TO_TICKS(
                    30
                )
            );

            continue;
        }


        // ----------------------------------------------------
        // ONLINE NORMAL STREAM:
        // Fill an HTTP batch from the RAM queue.
        // ----------------------------------------------------
        if (
            ramBatchCount
                <
                CLOUD_BATCH_SIZE
        ) {

            CloudSample incoming;


            while (
                ramBatchCount
                    <
                    CLOUD_BATCH_SIZE
                &&
                xQueueReceive(
                    cloudQueue,
                    &incoming,
                    0
                )
                ==
                pdTRUE
            ) {

                if (
                    ramBatchCount
                        ==
                        0
                ) {

                    firstRamBatchSampleMs =
                        now;
                }


                ramBatch[
                    ramBatchCount
                ] =
                    incoming;

                ramBatchCount++;
            }
        }


        bool shouldSendRam =
            ramBatchCount
                >=
                CLOUD_BATCH_SIZE
            ||
            (
                ramBatchCount
                    >
                    0
                &&
                now
                -
                firstRamBatchSampleMs
                >=
                HTTP_FLUSH_MS
            );


        if (
            shouldSendRam
            &&
            (long)(
                now
                -
                nextHttpAllowedMs
            )
            >=
            0
        ) {

            if (
                uploadBatchToRender(
                    ramBatch,
                    ramBatchCount
                )
            ) {

                successfulCloudBatches++;

                ramBatchCount =
                    0;

                firstRamBatchSampleMs =
                    0;

                nextHttpAllowedMs =
                    millis()
                    +
                    250;
            }

            else {

                failedCloudBatches++;

                // Keep this RAM batch intact and retry it later.
                nextHttpAllowedMs =
                    millis()
                    +
                    HTTP_RETRY_MS;
            }
        }


        vTaskDelay(
            pdMS_TO_TICKS(
                30
            )
        );
    }
}


// ============================================================
// DISPLAY
// ============================================================

void updateDisplay(
    float accelerationMagnitude,
    float rotationMagnitude
) {

    restoreMonitoringStatusIfNeeded(
        millis()
    );


    display.fillRect(
        0,
        40,
        320,
        100,
        TFT_BLACK
    );


    display.setTextColor(
        TFT_WHITE,
        TFT_BLACK
    );


    display.setCursor(
        5,
        42
    );

    display.printf(
        "A %.2f %.2f %.2f",
        accX,
        accY,
        accZ
    );


    display.setCursor(
        5,
        61
    );

    display.printf(
        "G %.0f %.0f %.0f",
        gyrX,
        gyrY,
        gyrZ
    );


    display.setCursor(
        5,
        80
    );

    display.printf(
        "|A| %.2fg |G| %.0f",
        accelerationMagnitude,
        rotationMagnitude
    );


    display.setCursor(
        5,
        99
    );

    display.printf(
        "Bat %u%% WiFi %s",
        (uint16_t)chargeLevel,
        WiFi.status()
            ==
            WL_CONNECTED
        ?
        "ON"
        :
        "OFF"
    );


    unsigned int ramCount =
        cloudQueue
            ?
            (unsigned int)
            uxQueueMessagesWaiting(
                cloudQueue
            )
            :
            0;


    display.setCursor(
        5,
        118
    );

    if (
        persistentStorageReady
    ) {

        display.printf(
            "RAM %u FLASH %lu",
            ramCount,
            persistentPendingSamples
        );
    }

    else {

        display.printf(
            "RAM %u FLASH ERR",
            ramCount
        );
    }


    display.setTextColor(
        localStatusColour,
        TFT_BLACK
    );


    display.setCursor(
        175,
        118
    );

    display.print(
        localStatusText
    );
}


// ============================================================
// SETUP
// ============================================================

void setup() {

    Serial.begin(
        115200
    );


    battery.begin();

    battery.enableCharge();

    chargeLevel =
        battery.getChargeLevel();


    display.begin();

    display.setRotation(
        1
    );

    display.fillScreen(
        TFT_BLACK
    );

    display.setTextColor(
        TFT_WHITE,
        TFT_BLACK
    );

    display.setTextDatum(
        TL_DATUM
    );

    display.drawString(
        "Nesso EDGE Safety",
        5,
        5
    );

    display.drawString(
        BLE_SERVER_NAME,
        5,
        23
    );


    if (
        !IMU.begin()
    ) {

        Serial.println(
            "IMU initialization failed"
        );

        display.fillScreen(
            TFT_RED
        );

        display.drawString(
            "IMU FAILED",
            5,
            5
        );


        while (true) {

            delay(
                1000
            );
        }
    }


    setupBLE();


// Mount LittleFS and recover any samples saved before a reset.
persistentStorageReady =
    setupPersistentStorage();


if (
    persistentStorageReady
) {

    Serial.println(
        "FLASH: persistent offline buffering enabled"
    );
}

else {

    Serial.println(
        "FLASH: RAM-only fallback enabled"
    );
}


    cloudQueue =
        xQueueCreate(
            CLOUD_QUEUE_CAPACITY,
            sizeof(
                CloudSample
            )
        );


    if (
        cloudQueue
            ==
            nullptr
    ) {

        Serial.println(
            "ERROR: could not create cloud queue"
        );

        display.fillScreen(
            TFT_RED
        );

        display.drawString(
            "QUEUE FAILED",
            5,
            5
        );


        while (true) {

            delay(
                1000
            );
        }
    }


    // Network task handles Wi-Fi and HTTP independently.
    BaseType_t taskCreated =
        xTaskCreate(
            networkTask,
            "NessoNetwork",
            12288,
            nullptr,
            1,
            &networkTaskHandle
        );


    if (
        taskCreated
            !=
            pdPASS
    ) {

        Serial.println(
            "ERROR: could not create network task"
        );

        display.fillScreen(
            TFT_RED
        );

        display.drawString(
            "NETWORK TASK FAILED",
            5,
            5
        );


        while (true) {

            delay(
                1000
            );
        }
    }


    Serial.println();
    Serial.println(
        "Nesso EDGE safety system ready"
    );

    Serial.println(
        "Local detector continues even without WiFi."
    );

    Serial.println(
        "Offline samples are persisted to flash and synced after reconnect."
    );

    Serial.println();
}


// ============================================================
// MAIN LOOP
// ============================================================

void loop() {

    unsigned long now =
        millis();


    if (
        now
        -
        lastSampleMs
        >=
        SAMPLE_INTERVAL_MS
    ) {

        // Advance by the intended period to reduce timing drift.
        lastSampleMs +=
            SAMPLE_INTERVAL_MS;


        // Resynchronise after an unusually long delay.
        if (
            now
            -
            lastSampleMs
            >
            1000
        ) {

            lastSampleMs =
                now;
        }


        bool accelReady =
            IMU.accelerationAvailable();

        bool gyroReady =
            IMU.gyroscopeAvailable();


        if (accelReady) {

            IMU.readAcceleration(
                accX,
                accY,
                accZ
            );
        }


        if (gyroReady) {

            IMU.readGyroscope(
                gyrX,
                gyrY,
                gyrZ
            );
        }


        if (
            accelReady
            &&
            gyroReady
        ) {

            sampleCounter++;


            float accelerationMagnitude =
                vectorMagnitude(
                    accX,
                    accY,
                    accZ
                );


            float rotationMagnitude =
                vectorMagnitude(
                    gyrX,
                    gyrY,
                    gyrZ
                );


            CloudSample currentSample =
                makeCurrentSample();


            // Always maintain 3 seconds of local full-rate history.
            saveToPreEventBuffer(
                currentSample
            );


            // Critical processing happens LOCALLY first.
            updateSafetyDetector(
                now,
                accelerationMagnitude,
                rotationMagnitude
            );


            // BLE remains available if a nearby receiver is used.
            sendBLE();


            // Choose which samples should be retained for the cloud.
            bool important =
                false;


            if (
                shouldQueueCurrentSample(
                    accelerationMagnitude,
                    rotationMagnitude,
                    now,
                    important
                )
            ) {

                enqueueCloudSample(
                    currentSample,
                    important
                );
            }


            if (
                forceHighResolutionUntilMs
                    !=
                    0
                &&
                (long)(
                    now
                    -
                    forceHighResolutionUntilMs
                )
                >=
                0
            ) {

                forceHighResolutionUntilMs =
                    0;
            }


            if (
                now
                -
                lastDisplayMs
                >=
                DISPLAY_INTERVAL_MS
            ) {

                lastDisplayMs =
                    now;


                updateDisplay(
                    accelerationMagnitude,
                    rotationMagnitude
                );
            }
        }
    }


    if (
        now
        -
        lastBatteryMs
        >=
        BATTERY_INTERVAL_MS
    ) {

        lastBatteryMs =
            now;

        chargeLevel =
            battery.getChargeLevel();
    }


    delay(
        1
    );
}
