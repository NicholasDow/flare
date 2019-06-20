import sys
import numpy as np
import datetime
import time
from typing import List
import copy
import multiprocessing as mp
import concurrent.futures
from flare import struc, gp, env, qe_util, md, output


class Validate:
    def __init__(self, qe_input: str, dt: float, number_of_steps: int,
                 dft_steps, gp: gp.GaussianProcess, pw_loc: str,
                 prev_pos_init: np.ndarray=None, par: bool=False, skip: int=0,
                 calculate_energy=False, output_name='otf_run.out', no_cpus=1):

        self.qe_input = qe_input
        self.dt = dt
        self.number_of_steps = number_of_steps
        self.gp = gp
        self.pw_loc = pw_loc
        self.skip = skip
        self.dft_step = True
        self.dft_steps = dft_steps

        # parse input file
        positions, species, cell, masses = \
            qe_util.parse_qe_input(self.qe_input)

        self.structure = struc.Structure(cell=cell, species=species,
                                         positions=positions,
                                         mass_dict=masses,
                                         prev_positions=prev_pos_init)

        self.noa = self.structure.positions.shape[0]
        self.atom_list = list(range(self.noa))
        self.curr_step = 0

        # initialize local energies
        if calculate_energy:
            self.local_energies = np.zeros(self.noa)
        else:
            self.local_energies = None

        self.dft_count = 0

        # set pred function
        if not par and not calculate_energy:
            self.pred_func = self.predict_on_structure
        elif par and not calculate_energy:
            self.pred_func = self.predict_on_structure_par
        elif not par and calculate_energy:
            self.pred_func = self.predict_on_structure_en
        elif par and calculate_energy:
            self.pred_func = self.predict_on_structure_par_en
        self.par = par

        self.output_name = output_name

        # set number of cpus for qe runs
        self.no_cpus = no_cpus

    def run(self):
        output.write_header(self.gp.cutoffs, self.gp.kernel_name, self.gp.hyps,
                            self.gp.algo, self.dt, self.number_of_steps,
                            self.structure, self.output_name, 1.)
        counter = 0
        self.start_time = time.time()

        while self.curr_step < self.number_of_steps:
            self.pred_func()
            self.dft_step = False
            new_pos = md.update_positions(self.dt, self.noa,
                                          self.structure)

            if self.curr_step in self.dft_steps:
                # record GP forces
                self.update_temperature(new_pos)
                self.record_state()

                # run DFT and record forces
                self.dft_step = True
                self.run_dft()
                new_pos = md.update_positions(self.dt, self.noa,
                                              self.structure)
                self.update_temperature(new_pos)
                self.record_state()

            # write gp forces
            if counter >= self.skip and not self.dft_step:
                self.update_temperature(new_pos)
                self.record_state()
                counter = 0

            counter += 1
            self.update_positions(new_pos)
            self.curr_step += 1

        output.conclude_run(self.output_name)

    def predict_on_atom(self, atom):
        chemenv = env.AtomicEnvironment(self.structure, atom, self.gp.cutoffs)
        comps = []
        stds = []
        # predict force components and standard deviations
        for i in range(3):
            force, var = self.gp.predict(chemenv, i+1)
            comps.append(float(force))
            stds.append(np.sqrt(np.abs(var)))

        return comps, stds

    def predict_on_atom_en(self, atom):
        chemenv = env.AtomicEnvironment(self.structure, atom, self.gp.cutoffs)
        comps = []
        stds = []
        # predict force components and standard deviations
        for i in range(3):
            force, var = self.gp.predict(chemenv, i+1)
            comps.append(float(force))
            stds.append(np.sqrt(np.abs(var)))

        # predict local energy
        local_energy = self.gp.predict_local_energy(chemenv)
        return comps, stds, local_energy

    def predict_on_structure_par(self):
        n = 0
        with concurrent.futures.ProcessPoolExecutor() as executor:
            for res in executor.map(self.predict_on_atom, self.atom_list):
                for i in range(3):
                    self.structure.forces[n][i] = res[0][i]
                    self.structure.stds[n][i] = res[1][i]
                n += 1

    def predict_on_structure_par_en(self):
        n = 0
        with concurrent.futures.ProcessPoolExecutor() as executor:
            for res in executor.map(self.predict_on_atom_en, self.atom_list):
                for i in range(3):
                    self.structure.forces[n][i] = res[0][i]
                    self.structure.stds[n][i] = res[1][i]
                self.local_energies[n] = res[2]
                n += 1

    def predict_on_structure(self):
        for n in range(self.structure.nat):
            chemenv = env.AtomicEnvironment(self.structure, n, self.gp.cutoffs)
            for i in range(3):
                force, var = self.gp.predict(chemenv, i + 1)
                self.structure.forces[n][i] = float(force)
                self.structure.stds[n][i] = np.sqrt(np.abs(var))

    def predict_on_structure_en(self):
        for n in range(self.structure.nat):
            chemenv = env.AtomicEnvironment(self.structure, n, self.gp.cutoffs)
            for i in range(3):
                force, var = self.gp.predict(chemenv, i + 1)
                self.structure.forces[n][i] = float(force)
                self.structure.stds[n][i] = np.sqrt(np.abs(var))
            self.local_energies[n] = self.gp.predict_local_energy(chemenv)

    def run_dft(self):
        output.write_to_output('\nCalling Quantum Espresso...\n',
                               self.output_name)

        # calculate DFT forces
        forces = qe_util.run_espresso_par(self.qe_input, self.structure,
                                          self.pw_loc, self.no_cpus)
        self.structure.forces = forces

        # write wall time of DFT calculation
        self.dft_count += 1
        output.write_to_output('QE run complete.\n', self.output_name)
        time_curr = time.time() - self.start_time
        output.write_to_output('number of DFT calls: %i \n' % self.dft_count,
                               self.output_name)
        output.write_to_output('wall time from start: %.2f s \n' % time_curr,
                               self.output_name)

    def update_positions(self, new_pos):
        self.structure.prev_positions = self.structure.positions
        self.structure.positions = new_pos
        self.structure.wrap_positions()

    def update_temperature(self, new_pos):
        KE, temperature, velocities = \
                md.calculate_temperature(new_pos, self.structure, self.dt,
                                         self.noa)
        self.KE = KE
        self.temperature = temperature
        self.velocities = velocities

    def record_state(self):
        output.write_md_config(self.dt, self.curr_step, self.structure,
                               self.temperature, self.KE,
                               self.local_energies, self.start_time,
                               self.output_name, self.dft_step,
                               self.velocities)
