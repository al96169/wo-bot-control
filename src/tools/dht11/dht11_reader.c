/*
 * DHT11 Reader v4 — 优化时序
 * sysfs(输出) + mmap(快速读取)
 * 用法:  sudo ./dht11_reader_v4 <gpio_sysfs_number>
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <errno.h>

#define GPIO_BASE    0x6000D000
#define MMAP_SIZE    0x1000
#define DEFAULT_GPIO 194
#define RETRIES      8

static volatile uint32_t *gp;
static uint64_t cntfrq = 0;

static void init_timer(void) {
    asm volatile("mrs %0, cntfrq_el0" : "=r"(cntfrq));
    if (cntfrq == 0) cntfrq = 19200000;
    fprintf(stderr, "[dht11] timer: %lu Hz\n", cntfrq);
}

static inline uint64_t cycles(void) {
    uint64_t val;
    asm volatile("mrs %0, cntvct_el0" : "=r"(val));
    return val;
}

static inline void delay_us(unsigned int us) {
    uint64_t target = cycles() + us * (cntfrq / 1000000);
    while (cycles() < target) { asm volatile("" ::: "memory"); }
}

static int mmap_init(void) {
    int fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) { perror("[dht11] open /dev/mem"); return -1; }
    gp = (volatile uint32_t*)mmap(NULL, MMAP_SIZE, PROT_READ|PROT_WRITE,
                                   MAP_SHARED, fd, GPIO_BASE);
    close(fd);
    if (gp == MAP_FAILED) { perror("[dht11] mmap"); return -1; }
    return 0;
}

static inline int gpio_read_mmap(int gpio_num) {
    int po = gpio_num / 32, bi = gpio_num % 32;
    volatile uint32_t *port = (volatile uint32_t*)((uint8_t*)gp + po * 0x100);
    return (port[0x30 / 4] >> bi) & 1;
}

static int sysfs_write(const char *path, const char *value) {
    int fd = open(path, O_WRONLY);
    if (fd < 0) return -1;
    ssize_t ret = write(fd, value, strlen(value));
    close(fd);
    return (ret > 0) ? 0 : -1;
}

static int gpio_sysfs_export(int gpio_num) {
    char buf[32];
    snprintf(buf, sizeof(buf), "%d", gpio_num);
    return sysfs_write("/sys/class/gpio/export", buf);
}

static int gpio_sysfs_unexport(int gpio_num) {
    char buf[32];
    snprintf(buf, sizeof(buf), "%d", gpio_num);
    return sysfs_write("/sys/class/gpio/unexport", buf);
}

static int gpio_sysfs_direction(int gpio_num, const char *dir) {
    char path[128];
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d/direction", gpio_num);
    return sysfs_write(path, dir);
}

static int gpio_sysfs_value(int gpio_num, int value) {
    char path[128];
    snprintf(path, sizeof(path), "/sys/class/gpio/gpio%d/value", gpio_num);
    return sysfs_write(path, value ? "1" : "0");
}

// ---- DHT11 协议 (bit timeout 放宽到 500us) ----
static int wait_level(int gpio_num, int level, int timeout_us) {
    uint64_t start = cycles();
    uint64_t tout = (uint64_t)timeout_us * (cntfrq / 1000000);
    while (gpio_read_mmap(gpio_num) != level) {
        if ((cycles() - start) > tout) return 0;
        asm volatile("" ::: "memory");
    }
    return 1;
}

static int measure_pulse_high(int gpio_num, int timeout_us) {
    uint64_t start = cycles();
    uint64_t tout = (uint64_t)timeout_us * (cntfrq / 1000000);
    while (gpio_read_mmap(gpio_num) == 1) {
        if ((cycles() - start) > tout) return -1;
        asm volatile("" ::: "memory");
    }
    return (int)((cycles() - start) * 1000000 / cntfrq);
}

static int read_dht11(int gpio_num, float *temp, float *humi) {
    uint8_t data[5] = {0};

    // 设为输出，拉高稳定
    gpio_sysfs_direction(gpio_num, "out");
    gpio_sysfs_value(gpio_num, 1);
    usleep(200000); // 200ms 稳定

    // 起始信号: LOW 20ms
    gpio_sysfs_value(gpio_num, 0);
    usleep(20000);

    // 起始信号: HIGH 30us → 然后立即切输入
    gpio_sysfs_value(gpio_num, 1);
    delay_us(30);

    // 切输入（释放总线），不额外等待
    gpio_sysfs_direction(gpio_num, "in");
    // 不 usleep！立即开始 mmap 读取

    // 等待 DHT 响应（timeout 放宽）
    if (!wait_level(gpio_num, 0, 500)) {
        fprintf(stderr, "[dht11] no LOW response, level=%d\n", gpio_read_mmap(gpio_num));
        return -1;
    }
    if (!wait_level(gpio_num, 1, 500)) {
        fprintf(stderr, "[dht11] no HIGH response\n");
        return -2;
    }
    if (!wait_level(gpio_num, 0, 500)) {
        fprintf(stderr, "[dht11] no LOW before data\n");
        return -3;
    }

    // 读 40 bits
    for (int b = 0; b < 5; b++) {
        for (int i = 7; i >= 0; i--) {
            if (!wait_level(gpio_num, 1, 500)) {
                fprintf(stderr, "[dht11] bit timeout b%d.%d LOW→HIGH\n", b, i);
                return -4;
            }
            int dur = measure_pulse_high(gpio_num, 500);
            if (dur < 0) {
                fprintf(stderr, "[dht11] pulse timeout b%d.%d\n", b, i);
                return -5;
            }
            if (dur > 40) data[b] |= (1 << i);
        }
    }

    // 校验
    if ((uint8_t)(data[0] + data[1] + data[2] + data[3]) != data[4]) {
        fprintf(stderr, "[dht11] checksum FAIL: b0=%d b1=%d b2=%d b3=%d sum=%d b4=%d\n",
                data[0], data[1], data[2], data[3],
                (data[0]+data[1]+data[2]+data[3])&0xFF, data[4]);
        return -6;
    }

    *humi = data[0] + data[1] * 0.1f;
    *temp = data[2] + data[3] * 0.1f;
    return 0;
}

int main(int argc, char *argv[]) {
    init_timer();

    int gpio_num = (argc > 1) ? atoi(argv[1]) : DEFAULT_GPIO;

    if (mmap_init() < 0) {
        printf("{\"error\": \"mmap_init_failed\"}\n");
        return 1;
    }

    gpio_sysfs_unexport(gpio_num);
    usleep(50000);
    gpio_sysfs_export(gpio_num);
    usleep(100000);

    float temp, humi;
    int ret = -1;

    for (int a = 0; a < RETRIES; a++) {
        ret = read_dht11(gpio_num, &temp, &humi);
        if (ret == 0) {
            fprintf(stderr, "[dht11] SUCCESS on attempt %d!\n", a+1);
            break;
        }
        fprintf(stderr, "[dht11] attempt %d failed code %d, waiting 2s...\n", a+1, ret);
        usleep(2000000);
    }

    gpio_sysfs_unexport(gpio_num);

    if (ret == 0) {
        printf("{\"temperature\": %.1f, \"humidity\": %.1f, \"gpio\": %d}\n", temp, humi, gpio_num);
        return 0;
    } else {
        printf("{\"error\": \"read_failed\", \"code\": %d, \"gpio\": %d}\n", ret, gpio_num);
        return 1;
    }
}
