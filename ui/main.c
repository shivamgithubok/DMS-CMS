#include "lvgl.h"

#include <X11/Xlib.h>
#include <X11/cursorfont.h>
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define UI_W 1280
#define UI_H 640
#define RAIL_X 1180
#define RAIL_W 100
#define PREVIEW_W 640
#define PREVIEW_H 360
#define PORT 9750

static lv_obj_t *screen_root;
static lv_obj_t *title;
static lv_obj_t *chip[4];
static lv_obj_t *chip_text[4];
static lv_obj_t *cms_card;
static lv_obj_t *cms_title;
static lv_obj_t *cms_pill;
static lv_obj_t *cms_line[5];
static lv_obj_t *sos_btn;
static lv_obj_t *dismiss_btn;
static lv_obj_t *live_view;
static uint8_t live_buf[PREVIEW_W * PREVIEW_H * 4] __attribute__((aligned(64)));
static struct timespec live_mtim;
static char mode[32] = "self_test";
static int sock_fd = -1;
static char acc[2048];
static size_t acc_len;
static int shot_index;
static int sos_pressed;
static uint32_t incident_seen_at;
static lv_obj_t *rail;
static lv_obj_t *rail_btn[2];
static lv_obj_t *driver_sheet;
static lv_obj_t *passenger_sheet;
static int profile_open;

typedef struct {
    char name[64];
    char id[32];
    char phone[32];
    char license_no[32];
    char license_class[16];
    char expiry[16];
    char contact[64];
    char contact_phone[32];
    char vehicle[32];
    char shift[16];
    char note[64];
} DriverProfile;

typedef struct {
    char name[64];
    char id[32];
    char phone[32];
    char seat[16];
    char contact[64];
    char contact_phone[32];
} PassengerProfile;

static DriverProfile driver_profile = {
    "Sample Driver", "DRV-1042", "+1 555 0100", "D-1042", "C", "2027-04-30",
    "Sample Contact", "+1 555 0199", "CAB-18", "06:00", "Must wear glasses",
};
static PassengerProfile passenger_profile = {
    "Sample Passenger", "PAX-220", "+1 555 0133", "Rear", "Sample Contact", "+1 555 0177",
};

static void mkdir_results(void)
{
    mkdir("results", 0755);
    mkdir("results/ui_shots", 0755);
}

static void write_cmd(const char *cmd)
{
    FILE *file = fopen("results/ui_cmd.txt", "w");
    if(!file) return;
    fprintf(file, "%s\n", cmd);
    fclose(file);
}

static void on_sos(lv_event_t *event)
{
    LV_UNUSED(event);
    write_cmd("sos");
}

static void on_dismiss(lv_event_t *event)
{
    LV_UNUSED(event);
    write_cmd("dismiss");
}

static lv_color_t chip_color(const char *value)
{
    if(strcmp(value, "ok") == 0 || strcmp(value, "saved") == 0) return lv_color_hex(0x1B7F4E);
    if(strcmp(value, "down") == 0) return lv_color_hex(0xA33B3B);
    if(strcmp(value, "planned") == 0 || strcmp(value, "no_fix") == 0) return lv_color_hex(0x8A5A12);
    return lv_color_hex(0x3A4450);
}

static void style_chip(lv_obj_t *obj, lv_color_t color)
{
    lv_obj_set_style_bg_color(obj, color, 0);
    lv_obj_set_style_bg_opa(obj, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(obj, 8, 0);
    lv_obj_set_style_border_width(obj, 0, 0);
    lv_obj_set_style_pad_all(obj, 0, 0);
}

static void set_mode_color(const char *next)
{
    uint32_t hex = 0x10241A;
    if(strcmp(next, "self_test") == 0) hex = 0x172033;
    else if(strcmp(next, "warning") == 0) hex = 0x3A2A08;
    else if(strcmp(next, "incident") == 0) hex = 0x3A1212;
    else if(strcmp(next, "sos") == 0) hex = 0x5A1018;
    lv_obj_set_style_bg_color(screen_root, lv_color_hex(hex), 0);
}

static void save_snapshot(const char *name)
{
    lv_obj_update_layout(screen_root);
    lv_refr_now(NULL);
    lv_draw_buf_t *snap = lv_snapshot_take(screen_root, LV_COLOR_FORMAT_XRGB8888);
    if(!snap) {
        fprintf(stderr, "[UI] snapshot failed for %s (%dx%d)\n", name,
                (int)lv_obj_get_width(screen_root), (int)lv_obj_get_height(screen_root));
        return;
    }

    char path[256];
    snprintf(path, sizeof(path), "results/ui_shots/%02d_%s.ppm", ++shot_index, name);
    FILE *file = fopen(path, "wb");
    if(!file) {
        lv_draw_buf_destroy(snap);
        return;
    }

    uint32_t width = snap->header.w;
    uint32_t height = snap->header.h;
    uint32_t stride = snap->header.stride;
    fprintf(file, "P6\n%u %u\n255\n", width, height);
    for(uint32_t y = 0; y < height; y++) {
        uint8_t *row = snap->data + (size_t)y * stride;
        for(uint32_t x = 0; x < width; x++) {
            uint8_t *px = row + (size_t)x * 4;
            fputc(px[2], file);
            fputc(px[1], file);
            fputc(px[0], file);
        }
    }
    fclose(file);
    lv_draw_buf_destroy(snap);
    printf("[UI] shot %s\n", path);
    fflush(stdout);
}

static void apply_state(char *line)
{
    char *fields[16];
    int count = 0;
    fields[0] = line;
    for(char *cursor = line; *cursor && count < 15; cursor++) {
        if(*cursor == '|') {
            *cursor = '\0';
            fields[++count] = cursor + 1;
        }
    }
    if(count < 15) return;

    int changed = strcmp(mode, fields[0]) != 0;
    snprintf(mode, sizeof(mode), "%s", fields[0]);
    set_mode_color(mode);

    const char *chip_values[4] = {fields[1], fields[2], fields[3], fields[4]};
    const char *chip_names[4] = {"CAM", "MIC", "GPS", "DMS"};
    for(int i = 0; i < 4; i++) {
        style_chip(chip[i], chip_color(chip_values[i]));
        lv_label_set_text_fmt(chip_text[i], "%s  %s", chip_names[i], chip_values[i]);
    }

    int watch = strncmp(fields[8], "gate:", 5) == 0 || strcmp(fields[8], "planned") == 0 || strcmp(fields[0], "incident") == 0 ||
                strcmp(fields[0], "warning") == 0 || strcmp(fields[0], "sos") == 0;
    lv_label_set_text(cms_pill, watch ? "Watch" : "Idle");
    lv_obj_set_style_bg_color(cms_pill, lv_color_hex(watch ? 0x8A5A12 : 0x1B7F4E), 0);
    lv_label_set_text_fmt(cms_line[0], "Front seats  %s", fields[12]);
    lv_label_set_text_fmt(cms_line[1], "Rear seats  %s", fields[13]);
    lv_label_set_text_fmt(cms_line[2], "Phone  %s", fields[14]);
    lv_label_set_text_fmt(cms_line[3], "Reaching  %s", fields[15]);
    lv_label_set_text_fmt(cms_line[4], "VLM  %s", fields[8]);

    if(changed) save_snapshot(mode);
    if((strcmp(mode, "incident") == 0 || strcmp(mode, "warning") == 0) && incident_seen_at == 0) {
        incident_seen_at = lv_tick_get();
        if(incident_seen_at == 0) incident_seen_at = 1;
    }
}

static void drain_socket(void)
{
    if(sock_fd < 0) {
        sock_fd = socket(AF_INET, SOCK_STREAM, 0);
        if(sock_fd < 0) return;
        struct sockaddr_in addr;
        memset(&addr, 0, sizeof(addr));
        addr.sin_family = AF_INET;
        addr.sin_port = htons(PORT);
        inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
        if(connect(sock_fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
            close(sock_fd);
            sock_fd = -1;
            return;
        }
    }

    char temp[512];
    for(;;) {
        ssize_t got = recv(sock_fd, temp, sizeof(temp), MSG_DONTWAIT);
        if(got == 0) {
            close(sock_fd);
            sock_fd = -1;
            return;
        }
        if(got < 0) {
            if(errno == EAGAIN || errno == EWOULDBLOCK) break;
            close(sock_fd);
            sock_fd = -1;
            return;
        }
        if(acc_len + (size_t)got >= sizeof(acc)) acc_len = 0;
        memcpy(acc + acc_len, temp, (size_t)got);
        acc_len += (size_t)got;
    }

    char *start = acc;
    char *end = memchr(start, '\n', acc_len);
    char *last = NULL;
    while(end) {
        *end = '\0';
        last = start;
        size_t used = (size_t)(end - acc) + 1;
        start = end + 1;
        end = memchr(start, '\n', acc_len - (size_t)(start - acc));
        if(!end) {
            apply_state(last);
            size_t keep = acc_len - used;
            memmove(acc, acc + used, keep);
            acc_len = keep;
            return;
        }
    }
}

static void poll_live(void)
{
    struct stat st;
    if(stat("results/live.bgr", &st) != 0) return;
    if(st.st_mtim.tv_sec == live_mtim.tv_sec && st.st_mtim.tv_nsec == live_mtim.tv_nsec) return;
    if(st.st_size != (off_t)(PREVIEW_W * PREVIEW_H * 3)) return;

    FILE *file = fopen("results/live.bgr", "rb");
    if(!file) return;
    uint8_t *bgr = malloc((size_t)st.st_size);
    if(!bgr) {
        fclose(file);
        return;
    }
    size_t got = fread(bgr, 1, (size_t)st.st_size, file);
    fclose(file);
    if(got != (size_t)st.st_size) {
        free(bgr);
        return;
    }

    for(int i = 0; i < PREVIEW_W * PREVIEW_H; i++) {
        live_buf[i * 4 + 0] = bgr[i * 3 + 0];
        live_buf[i * 4 + 1] = bgr[i * 3 + 1];
        live_buf[i * 4 + 2] = bgr[i * 3 + 2];
        live_buf[i * 4 + 3] = 0xFF;
    }
    free(bgr);
    live_mtim = st.st_mtim;
    lv_obj_invalidate(live_view);
}

static lv_obj_t *make_chip(int index, int col, int row, const char *text)
{
    lv_obj_t *obj = lv_obj_create(screen_root);
    lv_obj_set_size(obj, 210, 48);
    lv_obj_set_pos(obj, 688 + col * 230, 72 + row * 60);
    lv_obj_remove_flag(obj, LV_OBJ_FLAG_SCROLLABLE);
    style_chip(obj, lv_color_hex(0x3A4450));
    lv_obj_t *label = lv_label_create(obj);
    lv_label_set_text(label, text);
    lv_obj_set_style_text_color(label, lv_color_hex(0xF4F7FB), 0);
    lv_obj_set_style_text_font(label, &lv_font_montserrat_20, 0);
    lv_obj_center(label);
    chip_text[index] = label;
    return obj;
}

static lv_obj_t *make_button(const char *text, int x, lv_color_t color, lv_event_cb_t cb)
{
    lv_obj_t *btn = lv_button_create(screen_root);
    lv_obj_set_size(btn, 220, 64);
    lv_obj_set_pos(btn, x, 548);
    lv_obj_set_style_bg_color(btn, color, 0);
    lv_obj_set_style_radius(btn, 10, 0);
    lv_obj_add_event_cb(btn, cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *label = lv_label_create(btn);
    lv_label_set_text(label, text);
    lv_obj_set_style_text_font(label, &lv_font_montserrat_20, 0);
    lv_obj_center(label);
    return btn;
}

static void copy_field(char *dst, size_t size, const char *src)
{
    snprintf(dst, size, "%s", src ? src : "");
}

static int split_bar(char *line, char **out, int max)
{
    int count = 0;
    out[count++] = line;
    for(char *cursor = line; *cursor && count < max; cursor++) {
        if(*cursor == '|') {
            *cursor = '\0';
            out[count++] = cursor + 1;
        }
    }
    return count;
}

static void load_roster(void)
{
    FILE *file = fopen("fleet/roster.txt", "r");
    if(!file) return;
    char line[512];
    while(fgets(line, sizeof(line), file)) {
        char *start = line;
        while(*start == ' ' || *start == '\t') start++;
        if(*start == '#' || *start == '\n' || *start == '\0') continue;
        char *end = start + strlen(start);
        while(end > start && (end[-1] == '\n' || end[-1] == '\r')) {
            end--;
            *end = '\0';
        }
        char *fields[16];
        int count = split_bar(start, fields, 16);
        if(strcmp(fields[0], "driver") == 0 && count >= 12) {
            copy_field(driver_profile.name, sizeof(driver_profile.name), fields[1]);
            copy_field(driver_profile.id, sizeof(driver_profile.id), fields[2]);
            copy_field(driver_profile.phone, sizeof(driver_profile.phone), fields[3]);
            copy_field(driver_profile.license_no, sizeof(driver_profile.license_no), fields[4]);
            copy_field(driver_profile.license_class, sizeof(driver_profile.license_class), fields[5]);
            copy_field(driver_profile.expiry, sizeof(driver_profile.expiry), fields[6]);
            copy_field(driver_profile.contact, sizeof(driver_profile.contact), fields[7]);
            copy_field(driver_profile.contact_phone, sizeof(driver_profile.contact_phone), fields[8]);
            copy_field(driver_profile.vehicle, sizeof(driver_profile.vehicle), fields[9]);
            copy_field(driver_profile.shift, sizeof(driver_profile.shift), fields[10]);
            copy_field(driver_profile.note, sizeof(driver_profile.note), fields[11]);
        } else if(strcmp(fields[0], "passenger") == 0 && count >= 7) {
            copy_field(passenger_profile.name, sizeof(passenger_profile.name), fields[1]);
            copy_field(passenger_profile.id, sizeof(passenger_profile.id), fields[2]);
            copy_field(passenger_profile.phone, sizeof(passenger_profile.phone), fields[3]);
            copy_field(passenger_profile.seat, sizeof(passenger_profile.seat), fields[4]);
            copy_field(passenger_profile.contact, sizeof(passenger_profile.contact), fields[5]);
            copy_field(passenger_profile.contact_phone, sizeof(passenger_profile.contact_phone), fields[6]);
        }
    }
    fclose(file);
}

static void add_field(lv_obj_t *parent, int x, int y, int width, const char *label, const char *value)
{
    lv_obj_t *row = lv_obj_create(parent);
    lv_obj_set_pos(row, x, y);
    lv_obj_set_size(row, width, 24);
    lv_obj_remove_flag(row, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(row, lv_color_hex(0x101820), 0);
    lv_obj_set_style_bg_opa(row, LV_OPA_90, 0);
    lv_obj_set_style_border_width(row, 0, 0);
    lv_obj_set_style_radius(row, 4, 0);
    lv_obj_set_style_pad_all(row, 0, 0);

    lv_obj_t *key = lv_label_create(row);
    lv_label_set_text(key, label);
    lv_obj_set_style_text_color(key, lv_color_hex(0xB7C0CC), 0);
    lv_obj_set_style_text_font(key, &lv_font_montserrat_14, 0);
    lv_obj_set_pos(key, 8, 4);

    lv_obj_t *text = lv_label_create(row);
    lv_label_set_text(text, value);
    lv_obj_set_style_text_color(text, lv_color_hex(0xF7F9FC), 0);
    lv_obj_set_style_text_font(text, &lv_font_montserrat_14, 0);
    lv_obj_set_pos(text, 108, 4);
}

static lv_obj_t *make_sheet(int height)
{
    lv_obj_t *sheet = lv_obj_create(screen_root);
    lv_obj_set_size(sheet, 392, height);
    lv_obj_set_pos(sheet, RAIL_X - 408, 96);
    lv_obj_remove_flag(sheet, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(sheet, LV_OBJ_FLAG_HIDDEN);
    lv_obj_set_style_bg_color(sheet, lv_color_hex(0x1A2430), 0);
    lv_obj_set_style_bg_opa(sheet, LV_OPA_40, 0);
    lv_obj_set_style_border_color(sheet, lv_color_hex(0xD5DCE6), 0);
    lv_obj_set_style_border_width(sheet, 1, 0);
    lv_obj_set_style_radius(sheet, 12, 0);
    lv_obj_set_style_pad_all(sheet, 0, 0);
    return sheet;
}

static void style_rail_button(lv_obj_t *btn, int selected)
{
    lv_obj_set_style_bg_color(btn, lv_color_hex(selected ? 0x3D4C5C : 0x12181F), 0);
    lv_obj_set_style_bg_opa(btn, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(btn, 0, 0);
    lv_obj_set_style_radius(btn, 8, 0);
    lv_obj_set_style_shadow_width(btn, 0, 0);
}

static void show_profile(int which)
{
    profile_open = which;
    if(which == 1) lv_obj_remove_flag(driver_sheet, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_add_flag(driver_sheet, LV_OBJ_FLAG_HIDDEN);
    if(which == 2) lv_obj_remove_flag(passenger_sheet, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_add_flag(passenger_sheet, LV_OBJ_FLAG_HIDDEN);
    style_rail_button(rail_btn[0], which == 1);
    style_rail_button(rail_btn[1], which == 2);
    lv_obj_move_foreground(rail);
}

static void on_profile(lv_event_t *event)
{
    int which = (int)(intptr_t)lv_event_get_user_data(event);
    if(profile_open == which) which = 0;
    show_profile(which);
}

static lv_obj_t *make_rail_button(int index, int y, const char *text)
{
    lv_obj_t *btn = lv_button_create(rail);
    lv_obj_set_size(btn, 88, 72);
    lv_obj_set_pos(btn, 6, y);
    lv_obj_add_event_cb(btn, on_profile, LV_EVENT_CLICKED, (void *)(intptr_t)(index + 1));
    style_rail_button(btn, 0);
    lv_obj_t *label = lv_label_create(btn);
    lv_label_set_text(label, text);
    lv_obj_set_style_text_color(label, lv_color_hex(0xF4F7FB), 0);
    lv_obj_set_style_text_font(label, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_align(label, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_center(label);
    rail_btn[index] = btn;
    return btn;
}

static void build_profiles(void)
{
    load_roster();

    driver_sheet = make_sheet(390);
    lv_obj_t *driver_title = lv_label_create(driver_sheet);
    lv_label_set_text(driver_title, "DRIVER");
    lv_obj_set_style_text_color(driver_title, lv_color_hex(0xF7F9FC), 0);
    lv_obj_set_style_text_font(driver_title, &lv_font_montserrat_20, 0);
    lv_obj_set_style_bg_color(driver_title, lv_color_hex(0x101820), 0);
    lv_obj_set_style_bg_opa(driver_title, LV_OPA_90, 0);
    lv_obj_set_style_pad_hor(driver_title, 8, 0);
    lv_obj_set_style_pad_ver(driver_title, 2, 0);
    lv_obj_set_pos(driver_title, 16, 12);

    lv_obj_t *photo = lv_obj_create(driver_sheet);
    lv_obj_set_size(photo, 72, 88);
    lv_obj_set_pos(photo, 16, 48);
    lv_obj_remove_flag(photo, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(photo, lv_color_hex(0x101820), 0);
    lv_obj_set_style_bg_opa(photo, LV_OPA_80, 0);
    lv_obj_set_style_border_color(photo, lv_color_hex(0xD5DCE6), 0);
    lv_obj_set_style_border_width(photo, 1, 0);
    lv_obj_set_style_radius(photo, 8, 0);
    lv_obj_t *photo_label = lv_label_create(photo);
    lv_label_set_text(photo_label, "Photo\non file");
    lv_obj_set_style_text_color(photo_label, lv_color_hex(0xF7F9FC), 0);
    lv_obj_set_style_text_font(photo_label, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_align(photo_label, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_center(photo_label);

    add_field(driver_sheet, 100, 52, 276, "Name", driver_profile.name);
    add_field(driver_sheet, 100, 80, 276, "ID", driver_profile.id);
    add_field(driver_sheet, 100, 108, 276, "Phone", driver_profile.phone);
    add_field(driver_sheet, 16, 150, 360, "License", driver_profile.license_no);
    add_field(driver_sheet, 16, 178, 360, "Class", driver_profile.license_class);
    add_field(driver_sheet, 16, 206, 360, "Expiry", driver_profile.expiry);
    add_field(driver_sheet, 16, 234, 360, "Contact", driver_profile.contact);
    add_field(driver_sheet, 16, 262, 360, "Contact no", driver_profile.contact_phone);
    add_field(driver_sheet, 16, 290, 360, "Vehicle", driver_profile.vehicle);
    add_field(driver_sheet, 16, 318, 360, "Shift", driver_profile.shift);
    add_field(driver_sheet, 16, 346, 360, "Note", driver_profile.note);

    passenger_sheet = make_sheet(250);
    lv_obj_t *passenger_title = lv_label_create(passenger_sheet);
    lv_label_set_text(passenger_title, "PASSENGER");
    lv_obj_set_style_text_color(passenger_title, lv_color_hex(0xF7F9FC), 0);
    lv_obj_set_style_text_font(passenger_title, &lv_font_montserrat_20, 0);
    lv_obj_set_style_bg_color(passenger_title, lv_color_hex(0x101820), 0);
    lv_obj_set_style_bg_opa(passenger_title, LV_OPA_90, 0);
    lv_obj_set_style_pad_hor(passenger_title, 8, 0);
    lv_obj_set_style_pad_ver(passenger_title, 2, 0);
    lv_obj_set_pos(passenger_title, 16, 12);
    add_field(passenger_sheet, 16, 52, 360, "Name", passenger_profile.name);
    add_field(passenger_sheet, 16, 80, 360, "ID", passenger_profile.id);
    add_field(passenger_sheet, 16, 108, 360, "Phone", passenger_profile.phone);
    add_field(passenger_sheet, 16, 136, 360, "Seat", passenger_profile.seat);
    add_field(passenger_sheet, 16, 164, 360, "Contact", passenger_profile.contact);
    add_field(passenger_sheet, 16, 192, 360, "Contact no", passenger_profile.contact_phone);

    rail = lv_obj_create(screen_root);
    lv_obj_set_size(rail, RAIL_W, UI_H);
    lv_obj_set_pos(rail, RAIL_X, 0);
    lv_obj_remove_flag(rail, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(rail, lv_color_hex(0x12181F), 0);
    lv_obj_set_style_bg_opa(rail, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(rail, 0, 0);
    lv_obj_set_style_radius(rail, 0, 0);
    lv_obj_set_style_pad_all(rail, 0, 0);
    make_rail_button(0, 16, "DRIVER");
    make_rail_button(1, 100, "PASSENGER");
}

static void build_screen(void)
{
    screen_root = lv_screen_active();
    lv_obj_set_size(screen_root, UI_W, UI_H);
    lv_obj_set_style_bg_color(screen_root, lv_color_hex(0x172033), 0);
    lv_obj_set_style_bg_opa(screen_root, LV_OPA_COVER, 0);
    lv_obj_remove_flag(screen_root, LV_OBJ_FLAG_SCROLLABLE);

    title = lv_label_create(screen_root);
    lv_label_set_text(title, "CABIN MONITOR");
    lv_obj_set_style_text_color(title, lv_color_hex(0xF4F7FB), 0);
    lv_obj_set_style_text_font(title, &lv_font_montserrat_28, 0);
    lv_obj_set_pos(title, 24, 16);

    memset(live_buf, 0x20, sizeof(live_buf));
    live_view = lv_canvas_create(screen_root);
    lv_canvas_set_buffer(live_view, live_buf, PREVIEW_W, PREVIEW_H, LV_COLOR_FORMAT_XRGB8888);
    lv_obj_set_pos(live_view, 24, 64);
    lv_obj_set_style_border_color(live_view, lv_color_hex(0x3A4450), 0);
    lv_obj_set_style_border_width(live_view, 2, 0);

    chip[0] = make_chip(0, 0, 0, "CAM  --");
    chip[1] = make_chip(1, 1, 0, "MIC  --");
    chip[2] = make_chip(2, 0, 1, "GPS  --");
    chip[3] = make_chip(3, 1, 1, "DMS  --");

    cms_card = lv_obj_create(screen_root);
    lv_obj_set_pos(cms_card, 688, 200);
    lv_obj_set_size(cms_card, 460, 320);
    lv_obj_remove_flag(cms_card, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(cms_card, lv_color_hex(0x243044), 0);
    lv_obj_set_style_bg_opa(cms_card, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(cms_card, 0, 0);
    lv_obj_set_style_radius(cms_card, 12, 0);
    lv_obj_set_style_pad_all(cms_card, 16, 0);

    cms_title = lv_label_create(cms_card);
    lv_label_set_text(cms_title, "CMS  CABIN");
    lv_obj_set_style_text_color(cms_title, lv_color_hex(0xF4F7FB), 0);
    lv_obj_set_style_text_font(cms_title, &lv_font_montserrat_28, 0);
    lv_obj_set_pos(cms_title, 8, 8);

    cms_pill = lv_label_create(cms_card);
    lv_label_set_text(cms_pill, "Idle");
    lv_obj_set_style_text_color(cms_pill, lv_color_hex(0xF4F7FB), 0);
    lv_obj_set_style_text_font(cms_pill, &lv_font_montserrat_20, 0);
    lv_obj_set_style_bg_color(cms_pill, lv_color_hex(0x1B7F4E), 0);
    lv_obj_set_style_bg_opa(cms_pill, LV_OPA_COVER, 0);
    lv_obj_set_style_radius(cms_pill, 8, 0);
    lv_obj_set_style_pad_hor(cms_pill, 12, 0);
    lv_obj_set_style_pad_ver(cms_pill, 6, 0);
    lv_obj_align(cms_pill, LV_ALIGN_TOP_RIGHT, -8, 8);

    const char *cms_ready[5] = {
        "Front seats  --",
        "Rear seats  --",
        "Phone  --",
        "Reaching  --",
        "VLM  idle",
    };
    for(int i = 0; i < 5; i++) {
        cms_line[i] = lv_label_create(cms_card);
        lv_label_set_text(cms_line[i], cms_ready[i]);
        lv_obj_set_style_text_color(cms_line[i], lv_color_hex(0xF4F7FB), 0);
        lv_obj_set_style_text_font(cms_line[i], &lv_font_montserrat_20, 0);
        lv_obj_set_pos(cms_line[i], 8, 64 + i * 42);
    }

    sos_btn = make_button("SOS", 688, lv_color_hex(0xC62828), on_sos);
    dismiss_btn = make_button("Dismiss", 936, lv_color_hex(0x31404F), on_dismiss);
    build_profiles();
}

int main(void)
{
    mkdir_results();
    lv_init();
    lv_display_t *disp = lv_x11_window_create("CMS Cabin", UI_W, UI_H);
    lv_x11_inputs_create(disp, NULL);
    /* LVGL hides the X11 pointer and draws nothing when the cursor image is NULL. */
    {
        typedef struct {
            _x11_user_hdr_t hdr;
            Window window;
        } x11_window_hdr_t;
        x11_window_hdr_t *xd = lv_display_get_driver_data(disp);
        Cursor pointer = XCreateFontCursor(xd->hdr.display, XC_left_ptr);
        XDefineCursor(xd->hdr.display, xd->window, pointer);
        XFreeCursor(xd->hdr.display, pointer);
        XFlush(xd->hdr.display);
    }
    build_screen();
    printf("[UI] window open\n");
    fflush(stdout);

    uint32_t started = lv_tick_get();
    int boot_shot = 0;

    while(1) {
        lv_timer_handler();
        drain_socket();
        poll_live();
        if(!boot_shot && lv_tick_elaps(started) > 300) {
            boot_shot = 1;
            save_snapshot("boot");
        }
        if(!sos_pressed && incident_seen_at && getenv("CMS_UI_PRESS_SOS") &&
           lv_tick_elaps(incident_seen_at) > 1200) {
            sos_pressed = 1;
            lv_obj_send_event(sos_btn, LV_EVENT_CLICKED, NULL);
            printf("[UI] SOS button pressed\n");
            fflush(stdout);
        }
        usleep(5000);
    }
    return 0;
}
