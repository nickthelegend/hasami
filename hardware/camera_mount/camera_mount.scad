/*
Hasami top camera mount: deck mast + M3 tilt hinge + Camera Module 3 plate.
Units mm. Source for board: Raspberry Pi official Camera Module 3 mechanical
drawing https://datasheets.raspberrypi.com/camera/camera-module-3-standard-mechanical-drawing.pdf
Board 25 x 23.862; 4 x diameter 2.2 on 21 x 12.5 centres.
Set part to "mast" or "camera" to export each STL.
*/
$fn = 48;
part = "layout";            // "mast", "camera", "assembly", "clearance", or "layout"
tilt = 30;                  // downward angle, 0..45 degrees (assembly/clearance preview)
mast_height = 100;          // deck top to hinge axis; adjust after framing test
base_width = 60;
base_depth = 30;
base_thick = 6;
deck_hole_x = 40;           // four M3 through-bolts in deck, measured centre spacing
deck_hole_y = 16;
m3_clear = 3.4;
m2_clear = 2.3;
hinge_gap = 4.6;            // 4 mm plate tang + clearance
ear_thick = 4;
plate_w = 31;               // board width 25 + 3 mm margin each side
plate_thick = 3;
plate_y = 22;               // set camera plate ahead of ears, including at 45-degree tilt
board_hole_x = 21;
board_hole_y = 12.5;
board_bottom_hole = 4;      // from lower board edge in official drawing
plate_bottom = -8;          // board lower edge; ribbon exits freely downward
plate_top = 20;
hinge_d = 3.4;

assert(mast_height >= 80, "mast_height must be at least 80 mm for the strap slots");
assert(tilt >= 0 && tilt <= 45, "preview tilt must be within 0..45 degrees");

module mast() {
    difference() {
        union() {
            // foot on deck; round corners avoid sharp edges
            hull() for (x=[-base_width/2+4,base_width/2-4],
                        y=[-base_depth/2+4,base_depth/2-4])
                translate([x,y,0]) cylinder(r=4,h=base_thick);
            // vertical spine; top stops below pivot so tang can tilt
            translate([-6,-5,base_thick]) cube([12,10,mast_height-base_thick-23]);
            // two ears, hinge axis left-right (X)
            for (s=[-1,1])
                translate([s*(hinge_gap/2+ear_thick/2)-ear_thick/2,-9,mast_height-27])
                    cube([ear_thick,18,37]);
        }
        for (x=[-deck_hole_x/2,deck_hole_x/2],
             y=[-deck_hole_y/2,deck_hole_y/2])
            translate([x,y,-0.1]) cylinder(d=m3_clear,h=base_thick+0.2);
        // M3 through both ears
        translate([0,0,mast_height])
            rotate([0,90,0]) cylinder(d=hinge_d,h=30,center=true);
        // two pass-through slots for soft ribbon straps on the rear of the spine
        for (z=[30,65])
            translate([-3,-6,z]) cube([6,12,2.5]);
    }
}

module camera_plate() {
    difference() {
        union() {
            // plate behind the camera PCB; PCB faces +Y, cable exits bottom
            translate([-plate_w/2,plate_y,plate_bottom])
                cube([plate_w,plate_thick,plate_top-plate_bottom]);
            // single centre tang fits between mast ears
            translate([-2,-7,-7]) cube([4,plate_y+7.2,14]);
        }
        // M2 camera PCB holes: x +/-10.5; z -4 and 8.5
        for (x=[-board_hole_x/2,board_hole_x/2],
             z=[plate_bottom+board_bottom_hole,
                plate_bottom+board_bottom_hole+board_hole_y])
            translate([x,plate_y+plate_thick/2,z])
                rotate([90,0,0]) cylinder(d=m2_clear,h=6,center=true);
        translate([0,0,0])
            rotate([0,90,0]) cylinder(d=hinge_d,h=8,center=true);
    }
}

if (part == "mast") mast();
else if (part == "camera")
    // face down on bed: board face becomes Z=0
    translate([0,0,plate_y+plate_thick]) rotate([-90,0,0]) camera_plate();
else if (part == "assembly") {
    mast();
    translate([0,0,mast_height]) rotate([-tilt,0,0]) camera_plate();
}
else if (part == "clearance") {
    // Empty is the expected result: the printed parts must not intersect.
    intersection() {
        mast();
        translate([0,0,mast_height]) rotate([-tilt,0,0]) camera_plate();
    }
}
else {
    mast();
    translate([77,0,plate_y+plate_thick]) rotate([-90,0,0]) camera_plate();
}
